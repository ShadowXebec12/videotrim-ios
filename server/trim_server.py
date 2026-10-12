"""Video Trim server: lets the phone app browse this PC's video folders, preview a video,
and run the same ffmpeg cut as video_trim_gui.py. Output is saved on this PC.

The source video is only ever read. Output names never overwrite an existing file.
Everything specific to one machine (folders, port, key) lives in config.json next to this file.

Run: pythonw trim_server.py [--config path]
"""
import argparse
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULTS = {
    "port": 8776,
    "key": "",
    "roots": [],                                            # folders the phone may browse
    "out_dir": "",                                          # where trimmed files are saved
    "prefix": "trim_",
    "poses": [],                                            # names offered for the file name preset
    "blocked_names": [],                                    # folder names never listed, wherever they are
    "ffmpeg": "",                                           # filled in at first start
    "ffprobe": "",
    "max_entries": 3000,
}

CFG = {}
CFG_PATH = HERE / "config.json"
STATE_PATH = HERE / "edits.json"
LOG_PATH = HERE / "trim_server.log"
MAX_EDITS = 300
MAX_JOBS = 50
CHUNK = 256 * 1024

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts"}
PHONE_PLAYABLE = {".mp4", ".mov", ".m4v"}                   # what the iOS player can open
MIME = {".mp4": "video/mp4", ".m4v": "video/x-m4v", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
        ".webm": "video/webm", ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv", ".flv": "video/x-flv",
        ".ts": "video/mp2t"}

# ── copied from video_trim_gui.py so both tools cut and name files the same way ─
# name -> (extension, encoder args). {crf} is substituted with the quality value.
FORMATS = {
    "mp4 (h264/aac)": (".mp4", ["-c:v", "libx264", "-crf", "{crf}", "-preset", "veryfast",
                                "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    "mkv (h264/aac)": (".mkv", ["-c:v", "libx264", "-crf", "{crf}", "-preset", "veryfast",
                                "-pix_fmt", "yuv420p", "-c:a", "aac"]),
    "webm (vp9/opus)": (".webm", ["-c:v", "libvpx-vp9", "-crf", "{crf}", "-b:v", "0",
                                  "-c:a", "libopus"]),
    "gif": (".gif", ["-vf", "fps=15,scale=480:-1:flags=lanczos,split[s0][s1];"
                     "[s0]palettegen[p];[s1][p]paletteuse", "-loop", "0", "-an"]),
    "copy (no re-encode)": (None, ["-c", "copy"]),
}
COPY = "copy (no re-encode)"

def parse_ts(text):
    """'10:30' -> 630.0, '1:02:03.5' -> 3723.5, '90' -> 90.0. Raises ValueError."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text):
        raise ValueError("bad timestamp: %s" % text)
    parts = text.split(":")
    if len(parts) > 3:
        raise ValueError("bad timestamp: %s" % text)
    total = 0.0
    for part in parts:  # left-to-right: h:m:s, m:s, or s
        total = total * 60 + float(part)
    return total


def fmt_ts(seconds):
    """630.0 -> '10:30', 3723.5 -> '1:02:03.5'"""
    seconds = max(0.0, float(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    frac = ("%.2f" % (secs - int(secs))).rstrip("0").rstrip(".")[1:]
    body = "%d:%02d" % (mins, int(secs)) if not hrs else "%d:%02d:%02d" % (hrs, mins, int(secs))
    return body + frac


def tag_ts(seconds):
    """630.0 -> '1030' - compact suffix for filenames."""
    seconds = int(float(seconds))
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return ("%d%02d%02d" % (hrs, mins, secs)) if hrs else ("%02d%02d" % (mins, secs))


def unique_path(path):
    """Never clobber an existing file: foo.mp4 -> foo_2.mp4 -> foo_3.mp4"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists("%s_%d%s" % (stem, n, ext)):
        n += 1
    return "%s_%d%s" % (stem, n, ext)


def _pose_label(value):
    value = os.path.splitext(os.path.basename(value))[0].strip()
    value = re.sub(r"[_.]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" -_")
    value = re.sub(r"(?:\s*[-_]\s*|\s+)\d{4,}$", "", value).strip()
    if not value or not re.search(r"[A-Za-z]", value):
        return ""
    if len(value) > 80:
        return ""
    return value


def build_output_path(src, out_dir, prefix, start, fmt_name, add_tag=True, pose_name=""):
    ext, _ = FORMATS[fmt_name]
    stem, src_ext = os.path.splitext(os.path.basename(src))
    ext = ext or src_ext  # copy mode keeps the source container
    pose = _pose_label(pose_name)
    pose_part = (pose + "_") if pose else ""
    suffix = "_" + tag_ts(start) if add_tag else ""
    return unique_path(os.path.join(out_dir, "%s%s%s%s%s" % (prefix, pose_part, stem, suffix, ext)))


def build_cmd(src, dst, start, duration, fmt_name, crf, rotation=0, height=0, fps=0, bitrate_k=0):
    """Same command as the GUI when height/fps/bitrate_k are 0. Those three are the phone app's
    extra settings: output height in pixels, frames per second, video bitrate in kbit/s."""
    _, enc = FORMATS[fmt_name]
    enc = [a.replace("{crf}", str(crf)) for a in enc]
    filters = []
    rotation_filter = {90: "transpose=1", 180: "transpose=1,transpose=1",
                       270: "transpose=2"}.get(int(rotation) % 360)
    if rotation_filter:
        filters.append(rotation_filter)
    if height and fmt_name != "gif":          # gif has its own fixed 480 wide scale
        filters.append("scale=-2:%d" % int(height))
    if filters or fps or bitrate_k:
        if fmt_name == COPY:                  # a filter or new rate needs a re-encode
            enc = ["-c:v", "libx264", "-crf", str(crf), "-preset", "veryfast",
                   "-pix_fmt", "yuv420p", "-c:a", "aac"]
        if filters:
            chain = ",".join(filters)
            if "-vf" in enc:
                i = enc.index("-vf")
                enc[i + 1] = chain + "," + enc[i + 1]
            else:
                enc = ["-vf", chain] + enc
        if fps and fmt_name != "gif":
            enc = ["-r", "%g" % float(fps)] + enc
        if bitrate_k and "-crf" in enc:       # a fixed bitrate replaces the quality value
            i = enc.index("-crf")
            del enc[i:i + 2]
            if "-b:v" in enc:
                enc[enc.index("-b:v") + 1] = "%dk" % int(bitrate_k)
            else:
                enc[i:i] = ["-b:v", "%dk" % int(bitrate_k)]
    # -ss before -i seeks fast; with re-encode the cut is still frame-accurate.
    return ([CFG.get("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-ss", "%.3f" % start, "-i", src, "-t", "%.3f" % duration] + enc + [dst])


def probe_full(path):
    """{width, height, duration, fps, bitrate_k} or None if ffprobe can't read it."""
    try:
        out = subprocess.run(
            [CFG.get("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate:format=duration,bit_rate",
             "-of", "json", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=NO_WINDOW)
        data = json.loads(out.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        num, _, den = str(stream.get("r_frame_rate") or "0/1").partition("/")
        fps = float(num) / float(den or 1) if float(den or 1) else 0.0
        rate = fmt.get("bit_rate")
        return {"width": int(stream["width"]), "height": int(stream["height"]),
                "duration": float(fmt["duration"]), "fps": round(fps, 3),
                "bitrate_k": int(int(rate) / 1000) if str(rate or "").isdigit() else None}
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, subprocess.SubprocessError):
        return None


def probe(path):
    """(width, height, duration) or None, as in the GUI."""
    info = probe_full(path)
    return (info["width"], info["height"], info["duration"]) if info else None


# ── log, config ────────────────────────────────────────────────────────────────
def log(text):
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 200_000:
            LOG_PATH.write_text(LOG_PATH.read_text(encoding="utf-8", errors="replace")[-100_000:],
                                encoding="utf-8")
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def _write_json(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_config(path):
    global CFG_PATH, STATE_PATH, LOG_PATH
    CFG_PATH = path
    STATE_PATH, LOG_PATH = path.parent / "edits.json", path.parent / "trim_server.log"
    cfg = json.loads(json.dumps(DEFAULTS))
    user = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cfg.update(user)
    changed = not path.exists()
    if not cfg["key"]:
        cfg["key"] = secrets.token_urlsafe(18)
        changed = True
    # a scheduled task may start without ffmpeg on PATH, so the full path is kept in the config
    for tool in ("ffmpeg", "ffprobe"):
        if not cfg[tool] or not os.path.isfile(cfg[tool]):
            found = shutil.which(tool)
            if found and found != cfg[tool]:
                cfg[tool] = found
                changed = True
    if changed:
        _write_json(path, cfg)
    CFG.clear()
    CFG.update(cfg)
    EDITS.clear()
    if STATE_PATH.exists():
        try:
            EDITS.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except ValueError:
            log("edits.json unreadable, starting empty")


# ── paths the phone may touch ──────────────────────────────────────────────────
class Refused(Exception):
    pass


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _blocked(path):
    parts = [p.lower() for p in Path(path).parts]
    return any(part.startswith(name.lower()) for part in parts for name in CFG["blocked_names"])


def safe_path(path):
    """Absolute form of a path the phone sent, or Refused when it is outside the allowed folders."""
    if not path or "\x00" in path:
        raise Refused("no path given")
    full = os.path.abspath(path)
    low = _norm(full)
    for root in CFG["roots"]:
        base = _norm(root)
        if low == base or low.startswith(base.rstrip("\\/") + os.sep):
            if _blocked(full):
                raise Refused("that folder is blocked")
            return full
    raise Refused("outside the allowed folders")


def safe_video(path):
    full = safe_path(path)
    if os.path.splitext(full)[1].lower() not in VIDEO_EXTS:
        raise Refused("not a video file")
    if not os.path.isfile(full):
        raise Refused("file not found")
    return full


def safe_out_dir(path):
    if not CFG["out_dir"]:
        raise Refused("no output folder set in config.json")
    if not path or _norm(path) == _norm(CFG["out_dir"]):
        full = os.path.abspath(CFG["out_dir"])
    else:
        full = safe_path(path)
    if not os.path.isdir(full):
        raise Refused("output folder does not exist: %s" % full)
    return full


def browse(path):
    if not path:
        return {"ok": True, "path": "", "parent": None, "folders": [
            {"name": os.path.basename(r.rstrip("\\/")) or r, "path": os.path.abspath(r)}
            for r in CFG["roots"] if os.path.isdir(r)], "files": [], "out_dir": CFG["out_dir"]}
    full = safe_path(path)
    if not os.path.isdir(full):
        raise Refused("folder not found")
    folders, files, cut = [], [], False
    with os.scandir(full) as entries:
        for entry in entries:
            if len(folders) + len(files) >= CFG["max_entries"]:
                cut = True
                break
            try:
                if entry.is_dir():
                    if not _blocked(entry.path):
                        folders.append({"name": entry.name, "path": entry.path})
                elif os.path.splitext(entry.name)[1].lower() in VIDEO_EXTS:
                    stat = entry.stat()
                    files.append({"name": entry.name, "path": entry.path, "size": stat.st_size,
                                  "mtime": int(stat.st_mtime),
                                  "edited": _norm(entry.path) in EDITS})
            except OSError:
                continue
    folders.sort(key=lambda f: f["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    is_root = any(_norm(full) == _norm(r) for r in CFG["roots"])
    return {"ok": True, "path": full, "parent": "" if is_root else os.path.dirname(full),
            "folders": folders, "files": files, "cut": cut}


# ── saved edits (resume) ───────────────────────────────────────────────────────
EDITS = {}
EDIT_LOCK = threading.Lock()
EDIT_FIELDS = {"start": float, "end": float, "format": str, "crf": int, "rotation": int, "height": int,
               "fps": float, "bitrate_k": int, "prefix": str, "pose": str, "add_tag": bool, "out_dir": str}


def clean_edit(body):
    """Only the known settings, each as its own type. Raises ValueError on a bad value."""
    out = {}
    for key, kind in EDIT_FIELDS.items():
        if key not in body or body[key] is None:
            continue
        value = body[key]
        if kind is float and isinstance(value, str):
            value = parse_ts(value)
        if kind is bool:
            value = bool(value)
        else:
            value = kind(value)
        out[key] = value
    if "format" in out and out["format"] not in FORMATS:
        raise ValueError("unknown format: %s" % out["format"])
    for key in ("crf", "height", "fps", "bitrate_k", "start", "end"):
        if key in out and out[key] < 0:
            raise ValueError("%s cannot be negative" % key)
    if "crf" in out and out["crf"] > 63:
        raise ValueError("quality must be 0 to 63")
    for key in ("prefix", "pose"):
        if key in out and re.search(r'[\\/:*?"<>|\r\n]', out[key]):
            raise ValueError("%s has a character Windows does not allow in file names" % key)
    return out


def save_edit(src, settings, extra=None):
    with EDIT_LOCK:
        entry = EDITS.get(_norm(src), {})
        entry.update(settings)
        entry.update(extra or {})
        entry.update(path=src, updated=int(time.time()))
        EDITS[_norm(src)] = entry
        if len(EDITS) > MAX_EDITS:
            for key in sorted(EDITS, key=lambda k: EDITS[k].get("updated", 0))[:len(EDITS) - MAX_EDITS]:
                del EDITS[key]
        _write_json(STATE_PATH, EDITS)
        return dict(entry)


def recent():
    with EDIT_LOCK:
        rows = sorted(EDITS.values(), key=lambda e: -e.get("updated", 0))[:60]
    out = []
    for row in rows:
        if os.path.isfile(row.get("path", "")):
            out.append({**row, "name": os.path.basename(row["path"])})
    return out


# ── jobs: one ffmpeg at a time ─────────────────────────────────────────────────
JOBS = {}
JOB_LOCK = threading.Lock()
JOB_QUEUE = queue.Queue()
JOB_SEQ = [0]


def plan_job(body):
    """Validate a trim request and work out its command. Raises Refused/ValueError."""
    src = safe_video(str(body.get("path") or ""))
    settings = clean_edit(body)
    start, end = settings.get("start", 0.0), settings.get("end")
    if end is None:
        raise ValueError("no end time given")
    if end <= start:
        raise ValueError("End must be after start.")
    fmt_name = settings.get("format") or list(FORMATS)[0]
    out_dir = safe_out_dir(settings.get("out_dir") or "")
    dst = build_output_path(src, out_dir, settings.get("prefix", CFG["prefix"]), start, fmt_name,
                            settings.get("add_tag", True), settings.get("pose", ""))
    if _norm(dst) == _norm(src):
        raise ValueError("output would be the source file")
    cmd = build_cmd(src, dst, start, end - start, fmt_name, settings.get("crf", 18),
                    settings.get("rotation", 0), settings.get("height", 0), settings.get("fps", 0),
                    settings.get("bitrate_k", 0))
    return src, dst, cmd, settings, end - start


def start_job(body):
    src, dst, cmd, settings, duration = plan_job(body)
    with JOB_LOCK:
        JOB_SEQ[0] += 1
        job_id = "%d-%d" % (int(time.time()), JOB_SEQ[0])
        job = {"id": job_id, "status": "queued", "percent": 0, "message": "", "source": src,
               "output": dst, "name": os.path.basename(dst), "duration": duration, "cmd": cmd,
               "proc": None, "cancel": False}
        JOBS[job_id] = job
        for old in list(JOBS)[:-MAX_JOBS]:
            if JOBS[old]["status"] in ("done", "failed", "cancelled"):
                del JOBS[old]
    save_edit(src, settings)
    JOB_QUEUE.put(job_id)
    return public_job(job)


def public_job(job):
    return {k: job[k] for k in ("id", "status", "percent", "message", "source", "output", "name")}


def run_job(job):
    cmd = job["cmd"][:1] + ["-progress", "pipe:1", "-nostats"] + job["cmd"][1:]
    with JOB_LOCK:
        if job["cancel"]:
            job["status"] = "cancelled"
            return
        job["status"] = "running"
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", creationflags=NO_WINDOW)
        job["proc"] = proc
    errors = []
    reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
    reader.start()
    for line in proc.stdout:
        if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
            value = line.split("=", 1)[1].strip()
            if value.lstrip("-").isdigit() and job["duration"] > 0:
                job["percent"] = max(0, min(99, int(int(value) / 1_000_000 / job["duration"] * 100)))
    code = proc.wait()
    reader.join(timeout=5)
    proc.stdout.close()
    proc.stderr.close()
    job["proc"] = None
    if job["cancel"]:
        job["status"], job["message"] = "cancelled", "Cancelled."
        try:
            if os.path.isfile(job["output"]):      # the half-written file this job made itself
                os.remove(job["output"])
        except OSError:
            pass
        return
    if code != 0:
        text = (errors[0] if errors else "") or "ffmpeg failed"
        job["status"], job["message"] = "failed", text.strip().splitlines()[-1][:300]
        return
    info = probe(job["output"])
    if not info:
        job["status"], job["message"] = "failed", "ffmpeg reported success but the output is unreadable"
        return
    size = os.path.getsize(job["output"])
    job["percent"] = 100
    job["status"] = "done"
    job["message"] = "%s  (%.2fs, %dx%d, %.1f MB)" % (job["name"], info[2], info[0], info[1], size / 1048576)
    save_edit(job["source"], {}, {"last_output": job["output"]})


def worker():
    while True:
        job = JOBS.get(JOB_QUEUE.get())
        if not job:
            continue
        try:
            run_job(job)
        except Exception as error:
            job["status"], job["message"], job["proc"] = "failed", str(error)[:300], None
            log("job %s failed: %r" % (job["id"], error))
        log("job %s %s: %s" % (job["id"], job["status"], job["message"] or job["name"]))


def cancel_job(job_id):
    job = JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "unknown job"}
    with JOB_LOCK:
        if job["status"] not in ("queued", "running"):
            return {"ok": False, "error": "job already %s" % job["status"]}
        job["cancel"] = True
        proc = job["proc"]
        if proc and proc.poll() is None:
            proc.terminate()                       # only the ffmpeg this job started
    return {"ok": True}


# ── http ───────────────────────────────────────────────────────────────────────
FRAME_SLOTS = threading.BoundedSemaphore(2)


def frame_jpeg(path, t, width=640):
    cmd = [CFG.get("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % max(0.0, t),
           "-i", path, "-frames:v", "1", "-vf", "scale=%d:-2" % width, "-q:v", "5",
           "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    with FRAME_SLOTS:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                             creationflags=NO_WINDOW)
    return res.stdout if res.returncode == 0 and res.stdout[:2] == b"\xff\xd8" else None


def parse_range(header, size):
    """(first, last) for a 'bytes=' header, None for no/unusable header, False when unsatisfiable."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (header or "").strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    if not match.group(1):                         # last N bytes
        count = int(match.group(2))
        if count == 0 or size == 0:
            return False
        return max(0, size - count), size - 1
    first = int(match.group(1))
    last = int(match.group(2)) if match.group(2) else size - 1
    if first >= size or last < first:
        return False
    return first, min(last, size - 1)


class Handler(BaseHTTPRequestHandler):
    timeout = 60
    protocol_version = "HTTP/1.1"

    def reply(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authed(self, query):
        given = self.headers.get("X-Key") or query.get("key", [""])[0]
        return bool(CFG["key"]) and secrets.compare_digest(given.encode(), CFG["key"].encode())

    def body(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if not 0 < size <= 200_000:
                return {}
            data = json.loads(self.rfile.read(size).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def send_file(self, path, head_only=False):
        size = os.path.getsize(path)
        span = parse_range(self.headers.get("Range"), size)
        if span is False:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        first, last = span if span else (0, size - 1)
        length = max(0, last - first + 1)
        self.send_response(206 if span else 200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if span:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, size))
        self.end_headers()
        if head_only:
            return
        with open(path, "rb") as handle:
            handle.seek(first)
            left = length
            while left > 0:
                data = handle.read(min(CHUNK, left))
                if not data:
                    break
                self.wfile.write(data)
                left -= len(data)

    def handle_request(self, method):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        arg = lambda k: query.get(k, [""])[0]
        path = parsed.path
        body = self.body() if method == "POST" else {}
        if path == "/api/ping":
            return self.reply({"ok": True, "name": "videotrim"})
        if not self.authed(query):
            return self.reply({"ok": False, "error": "bad or missing key"}, 401)
        route = ("GET" if method == "HEAD" else method, path)
        if route == ("GET", "/video"):
            return self.send_file(safe_video(arg("path")), head_only=method == "HEAD")
        if method == "HEAD":
            return self.reply({"ok": False, "error": "not found"}, 404)
        if route == ("GET", "/frame"):
            data = frame_jpeg(safe_video(arg("path")), float(arg("t") or 0))
            if not data:
                return self.reply({"ok": False, "error": "no frame at that time"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(data)
        if route == ("GET", "/api/browse"):
            return self.reply(browse(arg("path")))
        if route == ("GET", "/api/options"):
            return self.reply({"ok": True, "formats": list(FORMATS), "poses": sorted(CFG["poses"], key=str.casefold),
                               "prefix": CFG["prefix"], "out_dir": CFG["out_dir"], "crf": 18,
                               "heights": [0, 2160, 1440, 1080, 720, 480, 360],
                               "fps": [0, 60, 30, 24, 16, 15]})
        if route == ("GET", "/api/info"):
            src = safe_video(arg("path"))
            info = probe_full(src)
            if not info:
                return self.reply({"ok": False, "error": "could not read this file with ffprobe"})
            with EDIT_LOCK:
                saved = dict(EDITS.get(_norm(src), {}))
            return self.reply({"ok": True, "path": src, "name": os.path.basename(src), **info,
                               "size": os.path.getsize(src),
                               "playable": os.path.splitext(src)[1].lower() in PHONE_PLAYABLE,
                               "edit": saved or None})
        if route == ("POST", "/api/state"):
            src = safe_video(str(body.get("path") or ""))
            return self.reply({"ok": True, "edit": save_edit(src, clean_edit(body))})
        if route == ("GET", "/api/recent"):
            return self.reply({"ok": True, "edits": recent()})
        if route == ("POST", "/api/name"):
            _, dst, _, _, _ = plan_job(body)
            return self.reply({"ok": True, "name": os.path.basename(dst), "output": dst})
        if route == ("POST", "/api/trim"):
            return self.reply({"ok": True, "job": start_job(body)})
        if route == ("GET", "/api/job"):
            job = JOBS.get(arg("id"))
            return self.reply({"ok": True, "job": public_job(job)} if job else {"ok": False, "error": "unknown job"})
        if route == ("POST", "/api/cancel"):
            return self.reply(cancel_job(arg("id") or str(body.get("id") or "")))
        return self.reply({"ok": False, "error": "not found"}, 404)

    def run(self, method):
        try:
            self.handle_request(method)
        except (BrokenPipeError, ConnectionError, TimeoutError):
            self.close_connection = True           # the phone stopped reading a video mid-stream
        except (Refused, ValueError) as error:
            try:
                self.reply({"ok": False, "error": str(error)[:300]}, 403 if isinstance(error, Refused) else 400)
            except OSError:
                pass
        except Exception as error:
            log("%s %s failed: %r" % (method, self.path.split("?")[0], error))
            self.close_connection = True
            try:
                self.reply({"ok": False, "error": str(error)[:300]}, 500)
            except OSError:
                pass

    def do_GET(self):
        self.run("GET")

    def do_HEAD(self):
        self.run("HEAD")

    def do_POST(self):
        self.run("POST")

    def log_message(self, *_):
        pass


class Server(ThreadingHTTPServer):
    allow_reuse_address = False   # a second copy fails to bind and exits instead of doubling up
    daemon_threads = True


def make_server(config_path, port=None):
    load_config(Path(config_path))
    server = Server(("0.0.0.0", CFG["port"] if port is None else port), Handler)
    threading.Thread(target=worker, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(HERE / "config.json"))
    args = parser.parse_args()
    try:
        server = make_server(args.config)
    except OSError:
        sys.exit(1)
    log("started on port %d, roots: %s" % (CFG["port"], "; ".join(CFG["roots"])))
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
