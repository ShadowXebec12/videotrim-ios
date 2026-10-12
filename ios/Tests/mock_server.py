"""Stand-in for trim_server.py used by the simulator tests. Same routes and answer shapes, canned
data, a real tiny mp4 and jpeg from Tests/assets, and it remembers what was posted (GET /_log).

Run: python3 mock_server.py [port]      key = test
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

KEY = "test"
LOG = []
ASSETS = Path(__file__).resolve().parent / "assets"
CLIP = (ASSETS / "clip.mp4").read_bytes()
FRAME = (ASSETS / "frame.jpg").read_bytes()
ROOT = "C:\\Videos"
FILES = {
    ROOT: {"folders": ["Clips", "Empty"], "files": []},
    ROOT + "\\Clips": {"folders": [], "files": ["alpha one.mp4", "beta.mkv"]},
    ROOT + "\\Empty": {"folders": [], "files": []},
}
EDITS = {}
JOBS = {}


def out_name(body):
    stem, _, ext = body["path"].rsplit("\\", 1)[-1].rpartition(".")
    fmt = body.get("format", "mp4 (h264/aac)")
    ext = "." + ext if fmt.startswith("copy") else "." + fmt.split(" ")[0]
    pose = (body.get("pose") or "") and body["pose"] + "_"
    start = int(float(body.get("start", 0)))
    tag = "_%02d%02d" % (start // 60, start % 60) if body.get("add_tag", True) else ""
    return "%s%s%s%s%s" % (body.get("prefix", "trim_"), pose, stem, tag, ext)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def blob(self, data, kind, head_only=False):
        size = len(data)
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        first, last, status = 0, size - 1, 200
        if match and (match.group(1) or match.group(2)):
            status = 206
            if match.group(1):
                first = int(match.group(1))
                last = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:
                first = max(0, size - int(match.group(2)))
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(last - first + 1))
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (first, last, size))
        self.end_headers()
        if not head_only:
            self.wfile.write(data[first:last + 1])

    def handle_any(self, method):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}") if size else {}
        path = parsed.path
        if path == "/_log":
            return self.reply(LOG)
        if path == "/api/ping":
            return self.reply({"ok": True, "name": "videotrim"})
        if (self.headers.get("X-Key") or query.get("key")) != KEY:
            return self.reply({"ok": False, "error": "bad or missing key"}, 401)
        if method == "POST":
            LOG.append({"path": path, "query": query, "body": body})
        route = ("GET" if method == "HEAD" else method, path)
        if route == ("GET", "/video"):
            return self.blob(CLIP, "video/mp4", method == "HEAD")
        if route == ("GET", "/frame"):
            return self.blob(FRAME, "image/jpeg")
        if route == ("GET", "/api/browse"):
            where = query.get("path", "")
            if not where:
                return self.reply({"ok": True, "path": "", "parent": None, "files": [], "out_dir": ROOT + "\\Out",
                                   "folders": [{"name": "Videos", "path": ROOT}]})
            if where not in FILES:
                return self.reply({"ok": False, "error": "outside the allowed folders"}, 403)
            node = FILES[where]
            return self.reply({"ok": True, "path": where, "parent": "", "cut": False,
                               "folders": [{"name": n, "path": where + "\\" + n} for n in node["folders"]],
                               "files": [{"name": n, "path": where + "\\" + n, "size": 5 * 1048576,
                                          "mtime": 1767348000, "edited": (where + "\\" + n) in EDITS}
                                         for n in node["files"]]})
        if route == ("GET", "/api/options"):
            return self.reply({"ok": True, "formats": ["mp4 (h264/aac)", "mkv (h264/aac)", "webm (vp9/opus)", "gif",
                                                       "copy (no re-encode)"],
                               "poses": ["pose a", "pose b"], "prefix": "trim_", "out_dir": ROOT + "\\Out", "crf": 18,
                               "heights": [0, 1080, 720, 480], "fps": [0, 60, 30, 24]})
        if route == ("GET", "/api/info"):
            where = query.get("path", "")
            return self.reply({"ok": True, "path": where, "name": where.rsplit("\\", 1)[-1], "width": 64, "height": 64,
                               "duration": 20.0, "fps": 10.0, "bitrate_k": 40, "size": 5 * 1048576,
                               "playable": where.lower().endswith(".mp4"), "edit": EDITS.get(where)})
        if route == ("POST", "/api/state"):
            EDITS[body["path"]] = {**EDITS.get(body["path"], {}), **body, "updated": int(time.time())}
            return self.reply({"ok": True, "edit": EDITS[body["path"]]})
        if route == ("GET", "/api/recent"):
            rows = sorted(EDITS.values(), key=lambda e: -e["updated"])
            return self.reply({"ok": True, "edits": [{**r, "name": r["path"].rsplit("\\", 1)[-1]} for r in rows]})
        if route == ("POST", "/api/name"):
            if float(body.get("end", 0)) <= float(body.get("start", 0)):
                return self.reply({"ok": False, "error": "End must be after start."}, 400)
            return self.reply({"ok": True, "name": out_name(body), "output": ROOT + "\\Out\\" + out_name(body)})
        if route == ("POST", "/api/trim"):
            job_id = "job%d" % (len(JOBS) + 1)
            JOBS[job_id] = {"id": job_id, "polls": 0, "name": out_name(body), "source": body["path"]}
            EDITS[body["path"]] = {**EDITS.get(body["path"], {}), **body, "updated": int(time.time())}
            return self.reply({"ok": True, "job": {"id": job_id, "status": "queued", "percent": 0, "message": "",
                                                   "source": body["path"], "output": "", "name": out_name(body)}})
        if route == ("GET", "/api/job"):
            job = JOBS.get(query.get("id"))
            if not job:
                return self.reply({"ok": False, "error": "unknown job"})
            job["polls"] += 1
            done = job["polls"] >= 2
            return self.reply({"ok": True, "job": {
                "id": job["id"], "status": "done" if done else "running", "percent": 100 if done else 50,
                "message": job["name"] + "  (3.00s, 64x64, 0.1 MB)" if done else "", "source": job["source"],
                "output": ROOT + "\\Out\\" + job["name"], "name": job["name"]}})
        if route == ("POST", "/api/cancel"):
            return self.reply({"ok": True})
        return self.reply({"ok": False, "error": "not found"}, 404)

    def do_GET(self):
        self.handle_any("GET")

    def do_HEAD(self):
        self.handle_any("HEAD")

    def do_POST(self):
        self.handle_any("POST")

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8776
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
