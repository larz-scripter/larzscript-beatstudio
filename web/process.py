"""beatstudio processing worker - run via cron every minute.
Picks up any session with status "uploaded", converts the recorded vocal
to WAV, runs it through the real larzscript beatstudio.lz pipeline
(track-import -> mix -> preview -> master), and drops the results where
Apache serves them as static files.
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
# main groove x4 -> fill -> main), not one bar looped identically -
# real dynamics, and it costs almost nothing extra: mix_to_stereo loops
# whichever track is SHORTER to match the longer one, so the master's
# render time is governed by the vocal's length (up to 30s), not the
# beat pattern's own length - only beat-render itself (~2s/bar) scales
# with bar count, and 8 bars is still a few seconds.
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


def process_session(session_id, d):
    status_path = os.path.join(d, "status.json")
    with open(status_path) as f:
        st = json.load(f)
    ext = st.get("raw_ext", "webm")
    raw_path = os.path.join(d, "vocal_raw." + ext)
    if not os.path.exists(raw_path):
        write_status(d, "error", message="no uploaded audio found")
        return

    write_status(d, "processing")

    vocal_wav = os.path.join(d, "vocal.wav")
    p = run(["ffmpeg", "-y", "-i", raw_path, "-ac", "1", "-ar", "22050", "-acodec", "pcm_s16le", vocal_wav])
    if p.returncode != 0 or not os.path.exists(vocal_wav):
        write_status(d, "error", message="couldn't decode the recording", detail=p.stdout[-800:])
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

    p = run(BEATSTUDIO + ["beat-render", "--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message="beat render failed", detail=p.stdout[-800:])
        return

    p = run(BEATSTUDIO + ["track-import", vocal_wav, "--name=vocal", "--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message="couldn't add your recording as a track", detail=p.stdout[-800:])
        return

    run(BEATSTUDIO + ["mix", "beat", "--gain=-1", "--file=" + project])
    run(BEATSTUDIO + ["mix", "vocal", "--gain=2", "--file=" + project])

    # preview/master cost scales with how long the recording is (up to
    # the page's own 30s cap) - measured live (see commit message): a
    # ~120s timeout was too tight even for an 8-bar beat + 6s vocal once
    # under real load, and a false "timed out" here throws away a render
    # that was seconds from finishing. Generous on purpose.
    p = run(BEATSTUDIO + ["preview", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="preview mix failed", detail=p.stdout[-800:])
        return

    p = run(BEATSTUDIO + ["master", "--price=2", "--low=2", "--mid=0.5", "--high=3", "--file=" + project], timeout=900)
    if p.returncode != 0:
        write_status(d, "error", message="mastering failed", detail=p.stdout[-800:])
        return

    out_dir = os.path.join(RESULTS_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(os.path.join(d, "preview.wav"), os.path.join(out_dir, "before.wav"))
    shutil.copyfile(os.path.join(d, "master_1.wav"), os.path.join(out_dir, "after.wav"))
    os.chmod(os.path.join(out_dir, "before.wav"), 0o644)
    os.chmod(os.path.join(out_dir, "after.wav"), 0o644)

    write_status(d, "done", before_url="/beatstudio/results/" + session_id + "/before.wav",
                 after_url="/beatstudio/results/" + session_id + "/after.wav")


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
        if st.get("status") != "uploaded":
            continue
        try:
            process_session(session_id, d)
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
