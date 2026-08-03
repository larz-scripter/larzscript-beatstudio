"""beatstudio backend - upload/status/params API for larzos.com/beatstudio/.
Stdlib only, matching the estate's other small backend services.
Accepts a recorded vocal clip, saves it; the separate beatstudio_process.py
cron picks it up, mixes + masters it with larzscript beatstudio.lz, and
writes the result where Apache serves it as a static file. Also accepts
mixer/master parameter updates (POST /api/params) so a visitor can re-render
with new track gain/pan/mute and EQ/compressor/loudness settings without
re-recording - process.py picks those up as a lighter "rerender_requested"
job (mix + remaster only, no re-decode/re-import).
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

# Server-side clamps - defense in depth, never trust the slider ranges the
# page itself enforces. Mirrors what beatstudio.lz's own commands accept.
GAIN_RANGE = (-24.0, 12.0)
PAN_RANGE = (-1.0, 1.0)
EQ_RANGE = (-12.0, 12.0)
THRESH_RANGE = (-40.0, 0.0)
RATIO_RANGE = (1.0, 10.0)
CEILING_RANGE = (-12.0, -0.1)


def clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


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


def normalize_params(raw):
    """Validates + clamps a params blob from the client into the exact
    shape process.py expects. Raises ValueError on anything malformed -
    the caller turns that into a 400, never half-applies a bad blob."""
    if not isinstance(raw, dict):
        raise ValueError("params must be an object")
    tracks_in = raw.get("tracks", {})
    if not isinstance(tracks_in, dict):
        raise ValueError("params.tracks must be an object")
    tracks = {}
    for name in ("beat", "vocal"):
        t = tracks_in.get(name, {})
        if not isinstance(t, dict):
            t = {}
        tracks[name] = {
            "gain": clamp(t.get("gain", 0.0), *GAIN_RANGE),
            "pan": clamp(t.get("pan", 0.0), *PAN_RANGE),
            "mute": bool(t.get("mute", False)),
        }
    m = raw.get("master", {})
    if not isinstance(m, dict):
        m = {}
    master = {
        "low": clamp(m.get("low", 1.5), *EQ_RANGE),
        "mid": clamp(m.get("mid", 0.0), *EQ_RANGE),
        "high": clamp(m.get("high", 2.0), *EQ_RANGE),
        "thresh": clamp(m.get("thresh", -14.0), *THRESH_RANGE),
        "ratio": clamp(m.get("ratio", 3.0), *RATIO_RANGE),
        "ceiling": clamp(m.get("ceiling", -1.0), *CEILING_RANGE),
    }
    return {"tracks": tracks, "master": master}


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
        if self.path.startswith("/api/upload"):
            return self._handle_upload()
        if self.path.startswith("/api/params"):
            return self._handle_params()
        self._json(404, {"error": "not found"})

    def _handle_upload(self):
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

    def _handle_params(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        qparams = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        session_id = qparams.get("session", "")
        if not SESSION_RE.match(session_id):
            return self._json(400, {"error": "bad session id"})

        d = session_dir(session_id)
        status_path = os.path.join(d, "status.json")
        if not os.path.exists(status_path):
            return self._json(404, {"error": "no session - record something first"})
        with open(status_path) as f:
            st = json.load(f)
        # Only a session that has already produced a result can be
        # re-rendered - there's nothing to remix before the first mastered
        # take exists (and process.py's rerender path assumes the project
        # + tracks are already set up).
        if st.get("status") not in ("done", "error", "rerender_requested"):
            return self._json(409, {"error": "still processing the first render - try again shortly"})

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 65536:
            return self._json(400, {"error": "bad content length"})
        body = self.rfile.read(length)
        try:
            raw = json.loads(body)
            params = normalize_params(raw)
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": "bad params: " + str(e)})

        tmp = os.path.join(d, "params.json.tmp")
        with open(tmp, "w") as f:
            json.dump(params, f)
        os.replace(tmp, os.path.join(d, "params.json"))

        write_status(session_id, "rerender_requested")
        self._json(200, {"status": "rerender_requested"})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("beatstudio backend listening on 127.0.0.1:%d" % PORT)
    server.serve_forever()
