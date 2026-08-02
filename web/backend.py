"""beatstudio backend - upload/status API for larzos.com/beatstudio/.
Stdlib only, matching the estate's other small backend services.
Accepts a recorded vocal clip, saves it; the separate beatstudio_process.py
cron picks it up, mixes + masters it with larzscript beatstudio.lz, and
writes the result where Apache serves it as a static file.
"""
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SESSIONS_DIR = "/root/beatstudio_sessions"
PORT = 8478
SESSION_RE = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

EXT_BY_TYPE = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
}


def session_dir(session_id):
    return os.path.join(SESSIONS_DIR, session_id)


def write_status(session_id, status, **extra):
    d = session_dir(session_id)
    os.makedirs(d, exist_ok=True)
    payload = {"status": status, "updated_at": time.time()}
    payload.update(extra)
    tmp = os.path.join(d, "status.json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, os.path.join(d, "status.json"))


class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/status"):
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
            session_id = params.get("session", "")
            if not SESSION_RE.match(session_id):
                return self._json(400, {"error": "bad session id"})
            status_path = os.path.join(session_dir(session_id), "status.json")
            if not os.path.exists(status_path):
                return self._json(200, {"status": "unknown"})
            with open(status_path) as f:
                return self._json(200, json.load(f))
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/upload"):
            return self._json(404, {"error": "not found"})
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        session_id = params.get("session", "")
        if not SESSION_RE.match(session_id):
            return self._json(400, {"error": "bad session id"})

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 25 * 1024 * 1024:
            return self._json(400, {"error": "bad content length (max 25MB)"})
        content_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip()
        ext = EXT_BY_TYPE.get(content_type, "webm")

        d = session_dir(session_id)
        os.makedirs(d, exist_ok=True)
        raw_path = os.path.join(d, "vocal_raw." + ext)
        remaining = length
        with open(raw_path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)

        write_status(session_id, "uploaded", raw_ext=ext)
        self._json(200, {"status": "uploaded"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("beatstudio backend listening on 127.0.0.1:%d" % PORT)
    server.serve_forever()
