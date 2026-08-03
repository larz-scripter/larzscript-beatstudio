"""beatstudio processing worker - run via cron every minute.

Two job types, distinguished by session status:
  "uploaded"            -> full pipeline: decode -> init project -> beat-render
                           -> track-import vocal -> mix (default levels) ->
                           preview -> master (PAID, first time only).
  "rerender_requested"  -> lighter job: mix (new gain/pan/mute) -> preview ->
                           remaster (FREE - beatstudio.lz only charges the
                           wallet once per project, see beatstudio.lz's
                           `master`/`remaster` commands) with new EQ/
                           compressor/loudness settings. Reuses the beat/
                           vocal tracks already rendered by the first pass -
                           no re-decode, no re-synthesis.
Both drop results where Apache serves them as static files.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time

SESSIONS_DIR = "/root/beatstudio_sessions"
APP_DIR = "/opt/beatstudio"
RESULTS_DIR = "/var/www/larzos/beatstudio/results"
BEATSTUDIO = ["larzscript", os.path.join(APP_DIR, "beatstudio.lz")]
LOCK_PATH = "/opt/beatstudio/.process.lock"

# Same bpm/patterns as the demo beat on the page, so vocals line up with
# what the visitor heard while recording. An 8-bar arrangement (intro ->
# main groove x4 -> fill -> main), not one bar looped identically - real
# dynamics. Bar-level pattern editing isn't exposed on the page (yet) -
# this pass exposes the mixer (per-track gain/pan/mute) and the full
# master chain (EQ/compressor/loudness) as real controls instead.
BPM = "140"
MAIN_STEPS = [
    ("kick", 0), ("kick", 6), ("kick", 8), ("kick", 11),
    ("snare", 4), ("snare", 12),
    ("clap", 4), ("clap", 12),
    ("hihat", 0), ("hihat", 2), ("hihat", 4), ("hihat", 6),
    ("hihat", 8), ("hihat", 10), ("hihat", 12), ("hihat", 14), ("hihat", 15),
]
NAMED_STEPS = [
    ("intro", "kick", 0), ("intro", "hihat", 0), ("intro", "hihat", 4),
    ("intro", "hihat", 8), ("intro", "hihat", 12),
    ("fill", "kick", 0), ("fill", "kick", 3), ("fill", "kick", 6), ("fill", "kick", 8),
    ("fill", "kick", 10), ("fill", "kick", 12), ("fill", "kick", 14),
    ("fill", "snare", 4), ("fill", "snare", 12),
    ("fill", "hihat", 0), ("fill", "hihat", 1), ("fill", "hihat", 2), ("fill", "hihat", 3),
    ("fill", "hihat", 4), ("fill", "hihat", 5), ("fill", "hihat", 6), ("fill", "hihat", 7),
    ("fill", "hihat", 8), ("fill", "hihat", 9), ("fill", "hihat", 10), ("fill", "hihat", 11),
    ("fill", "hihat", 12), ("fill", "hihat", 13), ("fill", "hihat", 14), ("fill", "hihat", 15),
]
ARRANGEMENT = "intro*2,main*4,fill,main"

DEFAULT_PARAMS = {
    "tracks": {
        "beat": {"gain": -1.0, "pan": 0.0, "mute": False},
        "vocal": {"gain": 2.0, "pan": 0.0, "mute": False},
    },
    "master": {"low": 2.0, "mid": 0.5, "high": 3.0, "thresh": -14.0, "ratio": 3.0, "ceiling": -1.0},
}

MASTER_PRICE = "2"


def write_status(d, status, **extra):
    payload = {"status": status, "updated_at": time.time()}
    payload.update(extra)
    tmp = os.path.join(d, "status.json.tmp")
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, os.path.join(d, "status.json"))


def run(cmd, timeout=180, **kw):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           universal_newlines=True, timeout=timeout, **kw)


def load_params(d):
    path = os.path.join(d, "params.json")
    if not os.path.exists(path):
        return DEFAULT_PARAMS
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return DEFAULT_PARAMS


def apply_mix(project, params, log_prefix, d):
    """Sets both tracks' gain/pan/mute from params. Returns the failed
    CompletedProcess on error, or None on success - callers write_status
    and return themselves so a partial mix state never gets reported as
    "done"."""
    for name in ("beat", "vocal"):
        t = params["tracks"][name]
        mute_flag = "--mute=true" if t["mute"] else "--unmute"
        p = run(BEATSTUDIO + ["mix", name, "--gain=" + str(t["gain"]), "--pan=" + str(t["pan"]),
                               mute_flag, "--file=" + project])
        if p.returncode != 0:
            write_status(d, "error", message=log_prefix + ": couldn't set '" + name + "' mix", detail=p.stdout[-800:])
            return p
    return None


def render_preview_and_master(project, params, d, is_first_master):
    p = run(BEATSTUDIO + ["preview", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="preview mix failed", detail=p.stdout[-800:])
        return False

    m = params["master"]
    master_flags = ["--low=" + str(m["low"]), "--mid=" + str(m["mid"]), "--high=" + str(m["high"]),
                     "--thresh=" + str(m["thresh"]), "--ratio=" + str(m["ratio"]), "--ceiling=" + str(m["ceiling"])]
    if is_first_master:
        cmd = BEATSTUDIO + ["master", "--price=" + MASTER_PRICE] + master_flags + ["--file=" + project]
    else:
        cmd = BEATSTUDIO + ["remaster"] + master_flags + ["--file=" + project]
    # A real full-chain render (5+ minutes of audio through native EQ/
    # compressor/limiter) measured ~30-50s on this box at 5-8 minutes -
    # generous on purpose, the same lesson the original timeout already
    # learned once (a too-tight timeout throwing away a render that was
    # seconds from finishing is worse than waiting).
    p = run(cmd, timeout=600)
    if p.returncode != 0:
        write_status(d, "error", message="mastering failed", detail=p.stdout[-800:])
        return False
    return True


def publish_results(project, d, session_id):
    out_dir = os.path.join(RESULTS_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)
    proj = json.load(open(project))
    masters = proj.get("masters", [])
    if not masters:
        write_status(d, "error", message="no master was produced")
        return
    last_master_path = masters[-1]["path"]
    shutil.copyfile(os.path.join(d, "preview.wav"), os.path.join(out_dir, "before.wav"))
    shutil.copyfile(last_master_path, os.path.join(out_dir, "after.wav"))
    os.chmod(os.path.join(out_dir, "before.wav"), 0o644)
    os.chmod(os.path.join(out_dir, "after.wav"), 0o644)
    write_status(d, "done", before_url="/beatstudio/results/" + session_id + "/before.wav",
                 after_url="/beatstudio/results/" + session_id + "/after.wav",
                 peak_dbfs=masters[-1]["peak_dbfs"])


def process_uploaded(session_id, d):
    status_path = os.path.join(d, "status.json")
    with open(status_path) as f:
        st = json.load(f)
    ext = st.get("raw_ext", "webm")
    raw_path = os.path.join(d, "vocal_raw." + ext)
    if not os.path.exists(raw_path):
        write_status(d, "error", message="no uploaded audio found")
        return

    write_status(d, "processing")

    vocal_wav = os.path.join(d, "vocal_upload.wav")
    p = run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path, "-ac", "1", "-ar", "22050",
             "-acodec", "pcm_s16le", vocal_wav])
    if p.returncode != 0 or not os.path.exists(vocal_wav):
        write_status(d, "error", message="couldn't decode the recording", detail=p.stdout[-800:] if p.stdout else "")
        return

    project = os.path.join(d, "project.json")
    p = run(BEATSTUDIO + ["init", "--budget=1000", "--bpm=" + BPM, "--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message="init failed", detail=p.stdout[-800:])
        return

    for voice, step in MAIN_STEPS:
        run(BEATSTUDIO + ["step", voice, str(step), "on", "--file=" + project])
    for pattern, voice, step in NAMED_STEPS:
        run(BEATSTUDIO + ["pattern-step", pattern, voice, str(step), "on", "--file=" + project])
    p = run(BEATSTUDIO + ["arrange", ARRANGEMENT, "--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message="arrangement failed", detail=p.stdout[-800:])
        return

    p = run(BEATSTUDIO + ["beat-render", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="beat render failed", detail=p.stdout[-800:])
        return

    p = run(BEATSTUDIO + ["track-import", vocal_wav, "--name=vocal", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="couldn't add your recording as a track", detail=p.stdout[-800:])
        return

    params = DEFAULT_PARAMS
    if apply_mix(project, params, "initial mix", d) is not None:
        return
    if not render_preview_and_master(project, params, d, is_first_master=True):
        return
    publish_results(project, d, session_id)


def process_rerender(session_id, d):
    project = os.path.join(d, "project.json")
    if not os.path.exists(project):
        write_status(d, "error", message="no project to remix - record something first")
        return
    write_status(d, "processing")
    params = load_params(d)
    if apply_mix(project, params, "remix", d) is not None:
        return
    if not render_preview_and_master(project, params, d, is_first_master=False):
        return
    publish_results(project, d, session_id)


def main():
    if not os.path.isdir(SESSIONS_DIR):
        return
    for session_id in os.listdir(SESSIONS_DIR):
        d = os.path.join(SESSIONS_DIR, session_id)
        status_path = os.path.join(d, "status.json")
        if not os.path.exists(status_path):
            continue
        try:
            with open(status_path) as f:
                st = json.load(f)
        except Exception:
            continue
        status = st.get("status")
        if status not in ("uploaded", "rerender_requested"):
            continue
        try:
            if status == "uploaded":
                process_uploaded(session_id, d)
            else:
                process_rerender(session_id, d)
        except Exception as e:
            write_status(d, "error", message="unexpected error: " + str(e))


if __name__ == "__main__":
    # A full pipeline run (ffmpeg + beat-render + mix + master) can take
    # longer than the cron's 1-minute tick, especially with several
    # sessions queued - without this lock, the next tick would start a
    # second overlapping instance racing the first over the same
    # project.json files. Non-blocking: if a previous run is still going,
    # this tick just exits and the next one tries again.
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    try:
        main()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
