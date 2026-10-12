"""Runs the real server on a spare port against a throwaway folder with a generated clip.
Nothing outside that temp folder is read or written.

Run: python test_trim_server.py
"""
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import trim_server as ts  # noqa: E402

GUI = HERE.parent.parent.parent / "video_trim_gui.py"
FAILS = []
COUNT = [0]


def check(what, ok, detail=""):
    COUNT[0] += 1
    if not ok:
        FAILS.append(what)
    print(("ok   " if ok else "FAIL ") + what + ((" -> " + str(detail)) if detail and not ok else ""))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Client:
    def __init__(self, port, key):
        self.base, self.key = "http://127.0.0.1:%d" % port, key

    def raw(self, path, query=None, body=None, headers=None, key=True, method=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        head = dict(headers or {})
        if key:
            head["X-Key"] = self.key if key is True else key
        if data is not None:
            head["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data, head, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def json(self, path, query=None, body=None, **kw):
        status, _, data = self.raw(path, query, body, **kw)
        return status, json.loads(data.decode("utf-8"))

    def wait(self, job_id, seconds=120):
        end = time.time() + seconds
        while time.time() < end:
            job = self.json("/api/job", {"id": job_id})[1]["job"]
            if job["status"] in ("done", "failed", "cancelled"):
                return job
            time.sleep(0.2)
        return {"status": "timeout", "message": ""}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="trimsrv_"))
    root, outside = tmp / "videos", tmp / "outside"
    sub = root / "sub folder é"
    out_dir = root / "out"
    blocked = root / "NAI gens before 4.30"
    for folder in (sub, out_dir, outside, blocked):
        folder.mkdir(parents=True)
    ts.load_config(tmp / "config.json")           # finds ffmpeg, writes a config with a fresh key
    ffmpeg = ts.CFG["ffmpeg"]
    check("ffmpeg and ffprobe found as full paths", os.path.isfile(ffmpeg) and os.path.isfile(ts.CFG["ffprobe"]))

    src = sub / "source clip ünï.mp4"             # space + non-ASCII on purpose
    gen = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           "testsrc=size=320x240:rate=30:duration=20", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
           "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart"]
    subprocess.run(gen + [str(src)], check=True, creationflags=ts.NO_WINDOW)
    mkv = root / "other.mkv"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-t", "4", "-c", "copy",
                    str(mkv)], check=True, creationflags=ts.NO_WINDOW)
    (root / "notes.txt").write_text("not a video", encoding="utf-8")
    (blocked / "x.mp4").write_bytes(src.read_bytes())
    (outside / "secret.mp4").write_bytes(src.read_bytes())
    src_hash = sha(src)

    config = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
    config.update(roots=[str(root)], out_dir=str(out_dir), poses=["side b", "pose a"],
                  blocked_names=["nai gens"])
    (tmp / "config.json").write_text(json.dumps(config), encoding="utf-8")
    with socket.socket() as probe_sock:
        probe_sock.bind(("127.0.0.1", 0))
        port = probe_sock.getsockname()[1]
    server = ts.make_server(tmp / "config.json", port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    c = Client(port, ts.CFG["key"])

    # ── auth ──
    check("ping needs no key", c.json("/api/ping", key=False) == (200, {"ok": True, "name": "videotrim"}))
    check("no key -> 401", c.json("/api/browse", key=False)[0] == 401)
    check("wrong key -> 401", c.json("/api/browse", key="nope")[0] == 401)
    check("key as query works (the phone player cannot send headers)",
          c.json("/api/browse", {"key": ts.CFG["key"]}, key=False)[0] == 200)

    # ── browse ──
    status, top = c.json("/api/browse")
    check("root list shows the one root", status == 200 and [f["path"] for f in top["folders"]] == [str(root)], top)
    status, listing = c.json("/api/browse", {"path": str(root)})
    names = [f["name"] for f in listing["folders"]]
    check("folders listed, blocked folder hidden", names == ["out", "sub folder é"], names)
    check("only video files listed", [f["name"] for f in listing["files"]] == ["other.mkv"], listing["files"])
    check("root has no parent above it", listing["parent"] == "")
    status, deep = c.json("/api/browse", {"path": str(sub)})
    check("subfolder with space + non-ASCII lists its clip",
          [f["name"] for f in deep["files"]] == [src.name] and deep["parent"] == str(root), deep)
    for label, bad in (("outside folder", str(outside)), ("dot-dot escape", str(root / ".." / "outside")),
                       ("blocked folder", str(blocked)), ("drive root", "C:\\"),
                       ("blocked file", str(blocked / "x.mp4"))):
        status, answer = c.json("/api/browse" if "file" not in label else "/api/info", {"path": bad})
        check("refused: " + label, status == 403 and not answer["ok"], (status, answer))
    check("video outside refused", c.raw("/video", {"path": str(outside / "secret.mp4")})[0] == 403)
    check("non-video file refused", c.raw("/video", {"path": str(root / "notes.txt")})[0] == 403)
    check("missing video refused", c.raw("/video", {"path": str(root / "nope.mp4")})[0] == 403)

    # ── info ──
    status, info = c.json("/api/info", {"path": str(src)})
    check("info reads size, duration, fps", info.get("ok") and (info["width"], info["height"]) == (320, 240)
          and abs(info["duration"] - 20) < 0.2 and abs(info["fps"] - 30) < 0.1 and info["playable"]
          and info["edit"] is None, info)
    check("mkv flagged not playable on the phone", c.json("/api/info", {"path": str(mkv)})[1]["playable"] is False)

    # ── video range ──
    blob = src.read_bytes()
    size = len(blob)
    status, head, data = c.raw("/video", {"path": str(src)})
    check("full file 200", status == 200 and data == blob and head.get("Accept-Ranges") == "bytes")
    status, head, data = c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=0-1"})
    check("range 0-1 -> 206, 2 bytes", status == 206 and data == blob[:2]
          and head.get("Content-Range") == "bytes 0-1/%d" % size, (status, head))
    status, head, data = c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=1000-4999"})
    check("middle range", status == 206 and data == blob[1000:5000] and head.get("Content-Length") == "4000")
    status, head, data = c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=%d-" % (size - 10)})
    check("open-ended range", status == 206 and data == blob[-10:])
    status, head, data = c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=-500"})
    check("suffix range", status == 206 and data == blob[-500:]
          and head.get("Content-Range") == "bytes %d-%d/%d" % (size - 500, size - 1, size))
    status, head, data = c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=0-99999999"})
    check("range past the end is clamped", status == 206 and data == blob)
    status, head, data = c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=%d-" % size})
    check("range starting past the end -> 416", status == 416)
    status, head, data = c.raw("/video", {"path": str(src)}, method="HEAD")
    check("HEAD gives the length and no body", status == 200 and head.get("Content-Length") == str(size) and not data)

    # ── frame ──
    status, head, data = c.raw("/frame", {"path": str(mkv), "t": "1.5"})
    check("frame is a JPEG", status == 200 and data[:2] == b"\xff\xd8" and head.get("Content-Type") == "image/jpeg")
    check("frame past the end -> 404", c.raw("/frame", {"path": str(mkv), "t": "999"})[0] == 404)

    # ── options, naming ──
    options = c.json("/api/options")[1]
    check("options list formats and poses", options["formats"] == list(ts.FORMATS) and options["poses"] == ["pose a", "side b"]
          and options["out_dir"] == str(out_dir))
    base = {"path": str(src), "start": 5, "end": 8, "format": "mp4 (h264/aac)", "crf": 18}
    check("name preview: prefix + stem + start tag",
          c.json("/api/name", body=base)[1].get("name") == "trim_source clip ünï_0005.mp4")
    check("name preview: pose preset + own prefix, no tag",
          c.json("/api/name", body={**base, "prefix": "x_", "pose": "pose a", "add_tag": False})[1].get("name")
          == "x_pose a_source clip ünï.mp4")
    check("timestamps accepted as text", c.json("/api/name", body={**base, "start": "0:05", "end": "0:08"})[1]["ok"])
    for label, bad in (("end before start", {**base, "end": 2}), ("unknown format", {**base, "format": "exe"}),
                       ("slash in prefix", {**base, "prefix": "..\\x"}), ("no end", {"path": str(src), "start": 1}),
                       ("bad number", {**base, "crf": "abc"}), ("negative height", {**base, "height": -5})):
        status, answer = c.json("/api/name", body=bad)
        check("rejected: " + label, status == 400 and not answer["ok"], (status, answer))
    status, answer = c.json("/api/name", body={**base, "out_dir": str(outside)})
    check("output folder outside the roots refused", status == 403, (status, answer))

    # ── jobs ──
    def run(extra, label):
        status, answer = c.json("/api/trim", body={**base, **extra})
        if not answer.get("ok"):
            check(label + ": accepted", False, answer)
            return None, None
        job = c.wait(answer["job"]["id"])
        check(label + ": done", job["status"] == "done", job)
        return job, (ts.probe_full(job["output"]) if job["status"] == "done" else None)

    job, got = run({}, "trim mp4")
    check("trim mp4: 3 s, same size, in the output folder", got and abs(got["duration"] - 3) < 0.15
          and (got["width"], got["height"]) == (320, 240) and Path(job["output"]).parent == out_dir, got)
    job2, _ = run({}, "same trim again")
    check("second run gets _2, first file kept", job2 and job2["name"] == "trim_source clip ünï_0005_2.mp4"
          and os.path.isfile(job["output"]), job2)
    job, got = run({"height": 120, "fps": 15, "prefix": "small_"}, "resolution + fps")
    check("resolution + fps applied", got and got["height"] == 120 and got["width"] == 160
          and abs(got["fps"] - 15) < 0.1, got)
    job, low = run({"bitrate_k": 60, "prefix": "lowbit_"}, "fixed bitrate")
    job, high = run({"crf": 0, "prefix": "hq_"}, "best quality")
    check("bitrate setting makes a smaller file than quality 0",
          low and high and os.path.getsize(c.json("/api/name", body={**base, "prefix": "lowbit_"})[1]["output"]
                                           .replace("_0005_2", "_0005")) < os.path.getsize(job["output"]))
    job, got = run({"rotation": 90, "prefix": "rot_"}, "rotate 90")
    check("rotation swaps width and height", got and (got["width"], got["height"]) == (240, 320), got)
    job, got = run({"format": "copy (no re-encode)", "prefix": "copy_", "start": 2, "end": 4}, "copy")
    check("copy keeps the source container", job and job["name"].endswith(".mp4"), job)
    job, got = run({"format": "copy (no re-encode)", "prefix": "copyscale_", "height": 120}, "copy + resolution")
    check("copy with a new resolution re-encodes", got and got["height"] == 120, got)
    job, got = run({"format": "webm (vp9/opus)", "prefix": "w_", "crf": 40, "end": 6}, "webm")
    job, got = run({"format": "gif", "prefix": "g_", "end": 6}, "gif")
    job, got = run({"path": str(mkv), "start": 1, "end": 2, "pose": "side b", "format": "mkv (h264/aac)"}, "mkv source")
    check("pose preset in the mkv output name", job and job["name"] == "trim_side b_other_0001.mkv", job)
    status, answer = c.json("/api/trim", body={**base, "start": 100, "end": 101, "prefix": "past_"})
    job = c.wait(answer["job"]["id"])
    check("start past the end fails cleanly", job["status"] == "failed" and job["message"], job)

    status, answer = c.json("/api/trim", body={**base, "start": 0, "end": 20, "format": "webm (vp9/opus)",
                                                "crf": 10, "prefix": "cancel_"})
    cancel_id, cancel_out = answer["job"]["id"], answer["job"]["output"]
    time.sleep(0.6)
    check("cancel accepted", c.json("/api/cancel", {"id": cancel_id}, body={})[1]["ok"])
    job = c.wait(cancel_id)
    check("cancelled job leaves no half file", job["status"] == "cancelled" and not os.path.exists(cancel_out), job)
    check("unknown job id", c.json("/api/job", {"id": "nope"})[1]["ok"] is False)

    # ── resume ──
    status, saved = c.json("/api/state", body={"path": str(mkv), "start": 1.25, "end": 3.5, "pose": "pose a",
                                                 "height": 720, "junk": "ignored"})
    check("state saved, unknown fields dropped", saved["ok"] and saved["edit"]["start"] == 1.25
          and "junk" not in saved["edit"], saved)
    edit = c.json("/api/info", {"path": str(src)})[1]["edit"]
    check("last trim settings come back with the video", edit and edit["start"] == 0 and edit["prefix"] == "cancel_"
          and edit.get("last_output"), edit)
    rows = c.json("/api/recent")[1]["edits"]
    check("recent lists both videos, newest first", [r["name"] for r in rows][:2] == [mkv.name, src.name]
          or [r["name"] for r in rows][:2] == [src.name, mkv.name], rows)
    check("browse marks edited files", c.json("/api/browse", {"path": str(root)})[1]["files"][0]["edited"] is True)
    ts.load_config(tmp / "config.json")           # what a restart does
    again = c.json("/api/info", {"path": str(mkv)})[1]["edit"]
    check("saved edit survives a restart", again and again["end"] == 3.5 and again["pose"] == "pose a", again)

    # ── same commands as the GUI ──
    if GUI.is_file():
        spec = importlib.util.spec_from_file_location("video_trim_gui", GUI)
        gui = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gui)
        same = True
        for fmt in gui.FORMATS:
            for rotation in (0, 90, 180, 270):
                mine = ts.build_cmd("a b.mp4", "o.mp4", 5, 3, fmt, 18, rotation)[1:]
                theirs = gui.build_cmd("a b.mp4", "o.mp4", 5, 3, fmt, 18, rotation)[1:]
                same = same and mine == theirs
        check("ffmpeg command identical to video_trim_gui.py for every format and rotation", same)
        check("formats identical to the GUI", gui.FORMATS == ts.FORMATS)
        names = all(os.path.basename(gui.build_output_path(str(src), str(tmp), p, s, f, t, pose))
                    == os.path.basename(ts.build_output_path(str(src), str(tmp), p, s, f, t, pose))
                    for p in ("trim_", "") for s in (5, 3723) for f in gui.FORMATS for t in (True, False)
                    for pose in ("", "pose a", "side b 2"))
        check("output names identical to the GUI", names)
    else:
        check("GUI found for the parity check", False, GUI)

    check("source file untouched", sha(src) == src_hash)

    # ── leaks: 200 requests of every kind ──
    me = psutil.Process()
    for _ in range(20):                            # warm up so first-use allocations are not counted
        c.json("/api/browse", {"path": str(root)})
    before = (me.num_handles(), me.memory_info().rss / 1048576, me.num_threads())
    for i in range(200):
        c.json("/api/browse", {"path": str(sub)})
        c.raw("/video", {"path": str(src)}, headers={"Range": "bytes=%d-%d" % (i * 100, i * 100 + 50000)})
        if i % 10 == 0:
            c.json("/api/info", {"path": str(src)})
            c.raw("/frame", {"path": str(mkv), "t": "1"})
        if i % 50 == 0:
            answer = c.json("/api/trim", body={**base, "prefix": "leak_", "end": 6})[1]
            c.wait(answer["job"]["id"])
    time.sleep(2)
    after = (me.num_handles(), me.memory_info().rss / 1048576, me.num_threads())
    print("handles %d -> %d, working set %.1f -> %.1f MB, threads %d -> %d"
          % (before[0], after[0], before[1], after[1], before[2], after[2]))
    check("handles back to baseline", after[0] - before[0] <= 8, (before, after))
    check("working set back to baseline", after[1] - before[1] <= 8, (before, after))

    server.shutdown()
    server.server_close()
    print("\n%d checks, %d failed%s" % (COUNT[0], len(FAILS), (": " + "; ".join(FAILS)) if FAILS else ""))
    print("test folder (left in place):", tmp)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
