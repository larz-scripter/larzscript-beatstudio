"""beatstudio backend - upload/status/params API for larzos.com/beatstudio/.
Stdlib only, matching the estate's other small backend services.
Accepts recorded vocal takes, saves them; the separate beatstudio_process.py
cron picks them up, assembles/mixes/masters them with larzscript
beatstudio.lz, and writes the result where Apache serves it as a static
file. Also accepts mixer/master parameter updates (POST /api/params) so a
visitor can re-render with new track gain/pan/mute and EQ/compressor/
loudness settings without re-recording - process.py picks those up as a
lighter "rerender_requested" job (mix + remaster only, no re-decode/
re-import).

Recording workspace (PLAN2.md Phase B/C) endpoints:
  POST /api/beat?session=X    {genre, bpm, bars} -> a free beat-only
                               preview render, before anything is recorded.
  POST /api/take?session=X&index=N   one raw take clip (record in bits).
  POST /api/assemble?session=X       {takes:[...], fx:{...}} -> kicks off
                               the same full pipeline /api/upload used to,
                               now via ffmpeg-assembling the takes (with
                               trim/gaps) into one vocal file first.
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

# Mirrors beatstudio.lz's own GENRES list - kept in sync by hand (small,
# rarely-changing list; not worth a shared-file mechanism for six names).
GENRES = ("trap", "lofi", "boombap", "pop", "afrobeat", "house")
BPM_RANGE = (60.0, 200.0)
BARS_RANGE = (1, 16)
MAX_TAKES = 40
TRIM_RANGE = (0.0, 600.0)
GAP_RANGE = (0.0, 30.0)
FADE_RANGE = (0.0, 10.0)


def clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def session_dir(session_id):
    return os.path.join(SESSIONS_DIR, session_id)


def current_status(session_id):
    status_path = os.path.join(session_dir(session_id), "status.json")
    if not os.path.exists(status_path):
        return None
    try:
        with open(status_path) as f:
            return json.load(f).get("status")
    except (json.JSONDecodeError, OSError):
        return None


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


def normalize_beat_request(raw):
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    genre = raw.get("genre")
    if genre not in GENRES:
        raise ValueError("genre must be one of " + ", ".join(GENRES))
    bpm = clamp(raw.get("bpm", 0), *BPM_RANGE)
    try:
        bars = int(raw.get("bars", 4))
    except (TypeError, ValueError):
        bars = 4
    bars = max(BARS_RANGE[0], min(BARS_RANGE[1], bars))
    # A real seed (not client-supplied - a visitor re-posting the same
    # {genre,bpm,bars} body should still be able to get a different take
    # by hitting "Generate" again, which only works if the server picks
    # the randomness, not the client).
    seed = int(time.time() * 1000) % 2147483647
    return {"genre": genre, "bpm": bpm, "bars": bars, "seed": seed}


def normalize_manifest(raw):
    """Validates + clamps an assemble-request body from the client.
    Raises ValueError on anything malformed - the caller turns that into
    a 400, never half-applies a bad manifest (matches normalize_params'
    fail-closed convention above)."""
    if not isinstance(raw, dict):
        raise ValueError("body must be an object")
    takes_in = raw.get("takes")
    if not isinstance(takes_in, list) or len(takes_in) == 0:
        raise ValueError("takes must be a non-empty array")
    if len(takes_in) > MAX_TAKES:
        raise ValueError("too many takes (max %d)" % MAX_TAKES)
    takes = []
    for t in takes_in:
        if not isinstance(t, dict):
            raise ValueError("each take must be an object")
        try:
            index = int(t["index"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("each take needs an integer index")
        if index < 0 or index >= MAX_TAKES:
            raise ValueError("take index out of range")
        trim_start = clamp(t.get("trimStart", 0.0), *TRIM_RANGE)
        trim_end = clamp(t.get("trimEnd", TRIM_RANGE[1]), *TRIM_RANGE)
        if trim_end <= trim_start:
            raise ValueError("take %d: trimEnd must be after trimStart" % index)
        gap_before = clamp(t.get("gapBefore", 0.0), *GAP_RANGE)
        takes.append({"index": index, "trimStart": trim_start, "trimEnd": trim_end, "gapBefore": gap_before})
    fx_in = raw.get("fx", {})
    if not isinstance(fx_in, dict):
        fx_in = {}
    fx = {
        "fadeIn": clamp(fx_in.get("fadeIn", 0.0), *FADE_RANGE),
        "fadeOut": clamp(fx_in.get("fadeOut", 0.0), *FADE_RANGE),
        "autotrim": bool(fx_in.get("autotrim", False)),
        "gate": bool(fx_in.get("gate", False)),
        "deess": bool(fx_in.get("deess", False)),
    }
    return {"takes": takes, "fx": fx}


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
        if self.path.startswith("/api/take"):
            return self._handle_take()
        if self.path.startswith("/api/assemble"):
            return self._handle_assemble()
        if self.path.startswith("/api/beat"):
            return self._handle_beat()
        if self.path.startswith("/api/params"):
            return self._handle_params()
        self._json(404, {"error": "not found"})

    def _read_body_to_file(self, dst_path, max_bytes):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > max_bytes:
            return None
        remaining = length
        with open(dst_path, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        return length

    def _handle_upload(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        session_id = params.get("session", "")
        if not SESSION_RE.match(session_id):
            return self._json(400, {"error": "bad session id"})

        content_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip()
        ext = EXT_BY_TYPE.get(content_type, "webm")
        d = session_dir(session_id)
        os.makedirs(d, exist_ok=True)
        raw_path = os.path.join(d, "vocal_raw." + ext)
        n = self._read_body_to_file(raw_path, 25 * 1024 * 1024)
        if n is None:
            return self._json(400, {"error": "bad content length (max 25MB)"})

        write_status(session_id, "uploaded", raw_ext=ext)
        self._json(200, {"status": "uploaded"})

    def _handle_take(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        qparams = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        session_id = qparams.get("session", "")
        if not SESSION_RE.match(session_id):
            return self._json(400, {"error": "bad session id"})
        try:
            index = int(qparams.get("index", "-1"))
        except ValueError:
            index = -1
        if index < 0 or index >= MAX_TAKES:
            return self._json(400, {"error": "bad take index"})

        content_type = self.headers.get("Content-Type", "audio/webm").split(";")[0].strip()
        ext = EXT_BY_TYPE.get(content_type, "webm")
        d = session_dir(session_id)
        os.makedirs(d, exist_ok=True)
        raw_path = os.path.join(d, "take_%d_raw.%s" % (index, ext))
        n = self._read_body_to_file(raw_path, 25 * 1024 * 1024)
        if n is None:
            return self._json(400, {"error": "bad content length (max 25MB)"})
        self._json(200, {"status": "ok", "index": index})

    def _handle_assemble(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        qparams = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        session_id = qparams.get("session", "")
        if not SESSION_RE.match(session_id):
            return self._json(400, {"error": "bad session id"})

        # A real race caught while testing: process.py's cron job writes
        # its TERMINAL status unconditionally when it finishes, with no
        # idea a newer request already moved this session on to
        # something else. If a beat-only render is still "processing"
        # when /api/assemble lands and sets "uploaded", the beat job
        # finishing afterward silently clobbers "uploaded" back to
        # "beat_ready" - the real upload never gets picked up. Reject
        # instead of racing, same convention _handle_params already uses
        # below for exactly this reason.
        if current_status(session_id) == "processing":
            return self._json(409, {"error": "still processing your beat - try again in a few seconds"})

        d = session_dir(session_id)
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 262144:
            return self._json(400, {"error": "bad content length"})
        body = self.rfile.read(length)
        try:
            raw = json.loads(body)
            manifest = normalize_manifest(raw)
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": "bad manifest: " + str(e)})

        # Every referenced take must actually have been uploaded via
        # /api/take first - fail closed rather than silently skipping a
        # missing clip mid-assembly (process.py would rather never start
        # than build a song with a take quietly missing).
        for t in manifest["takes"]:
            found = False
            for ext in EXT_BY_TYPE.values():
                if os.path.exists(os.path.join(d, "take_%d_raw.%s" % (t["index"], ext))):
                    found = True
                    break
            if not found:
                return self._json(400, {"error": "take %d was never uploaded" % t["index"]})

        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "manifest.json.tmp")
        with open(tmp, "w") as f:
            json.dump(manifest, f)
        os.replace(tmp, os.path.join(d, "manifest.json"))

        write_status(session_id, "uploaded")
        self._json(200, {"status": "uploaded"})

    def _handle_beat(self):
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        qparams = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        session_id = qparams.get("session", "")
        if not SESSION_RE.match(session_id):
            return self._json(400, {"error": "bad session id"})

        # Same race as _handle_assemble's guard, mirrored: a job already
        # "processing" (e.g. the full mix/master pipeline from a
        # previous take upload) would have its eventual done/error
        # clobbered by this request's "beat_requested" landing mid-run,
        # and then the beat job's own terminal write clobbers THAT right
        # back once it runs - either way one of the two outcomes is lost.
        if current_status(session_id) == "processing":
            return self._json(409, {"error": "still processing - try again in a few seconds"})

        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 4096:
            return self._json(400, {"error": "bad content length"})
        body = self.rfile.read(length)
        try:
            raw = json.loads(body)
            beat = normalize_beat_request(raw)
        except (ValueError, json.JSONDecodeError) as e:
            return self._json(400, {"error": "bad request: " + str(e)})

        d = session_dir(session_id)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "beat.json.tmp")
        with open(tmp, "w") as f:
            json.dump(beat, f)
        os.replace(tmp, os.path.join(d, "beat.json"))

        write_status(session_id, "beat_requested")
        self._json(200, {"status": "beat_requested"})

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
