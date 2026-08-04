"""beatstudio processing worker - run via cron every minute.

Job types, distinguished by session status:
  "beat_requested"      -> free, no wallet involved: generate + beat-render
                           + preview just the beat (beat.json's genre/bpm/
                           bars), so a visitor can hear/regenerate it BEFORE
                           committing to record - PLAN2.md Phase C.
  "uploaded"             -> full pipeline: assemble takes (if a recording-
                           workspace manifest.json is present) or decode the
                           legacy single upload -> init project -> generate
                           the beat -> track-import vocal -> voice-edit
                           (fade/autotrim/gate/de-ess, if requested) -> mix
                           (default levels) -> preview -> master (PAID,
                           first time only).
  "rerender_requested"  -> lighter job: mix (new gain/pan/mute) -> preview ->
                           remaster (FREE - beatstudio.lz only charges the
                           wallet once per project, see beatstudio.lz's
                           `master`/`remaster` commands) with new EQ/
                           compressor/loudness settings. Reuses the beat/
                           vocal tracks already rendered by the first pass -
                           no re-decode, no re-synthesis.
All drop results where Apache serves them as static files.
"""
import fcntl
import glob
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

# Used whenever a session has no beat.json (e.g. an old client, or a
# visitor who hits record before ever calling /api/beat) - a fixed,
# always-available fallback rather than a hard failure. bars=4 matches
# the arrangement length ("intro*2,main*4,fill,main*2,outro") the
# original hand-authored demo beat used.
DEFAULT_BEAT = {"genre": "boombap", "bpm": 140.0, "bars": 4, "seed": 1}

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


def load_beat(d):
    path = os.path.join(d, "beat.json")
    if not os.path.exists(path):
        return DEFAULT_BEAT
    try:
        with open(path) as f:
            b = json.load(f)
        return {"genre": b.get("genre", DEFAULT_BEAT["genre"]), "bpm": b.get("bpm", DEFAULT_BEAT["bpm"]),
                "bars": b.get("bars", DEFAULT_BEAT["bars"]), "seed": b.get("seed", DEFAULT_BEAT["seed"])}
    except (json.JSONDecodeError, OSError):
        return DEFAULT_BEAT


def generate_beat(project, beat, timeout=120):
    return run(BEATSTUDIO + ["generate", beat["genre"], "--bpm=" + str(beat["bpm"]),
                              "--bars=" + str(beat["bars"]), "--seed=" + str(beat["seed"]),
                              "--file=" + project], timeout=timeout)


def find_take_file(d, index):
    for path in glob.glob(os.path.join(d, "take_%d_raw.*" % index)):
        return path
    return None


# Assembles a manifest's takes (each independently trimmed, with a gap of
# silence before it) into ONE continuous mono 22050Hz WAV via ffmpeg -
# exactly the disclosed "format/container handling only" role ffmpeg
# already plays elsewhere in this app (decode_to_wav in beatstudio.lz);
# the actual audio EDITING (fade/autotrim/gate/de-ess) happens afterward,
# through voice-edit's real Larzscript DSP, never through an ffmpeg audio
# filter - see PLAN2.md's Phase B architecture note for why that split
# matters here.
def assemble_takes(d, manifest, out_path, log_prefix):
    takes = manifest["takes"]
    inputs = []
    filter_parts = []
    concat_labels = []
    for i, t in enumerate(takes):
        src = find_take_file(d, t["index"])
        if src is None:
            return "take %d file missing on disk" % t["index"]
        inputs += ["-i", src]
        input_idx = len(inputs) // 2 - 1
        dur = t["trimEnd"] - t["trimStart"]
        if t["gapBefore"] > 0:
            glabel = "g%d" % i
            filter_parts.append("anullsrc=r=22050:cl=mono:d=%.3f[%s]" % (t["gapBefore"], glabel))
            concat_labels.append(glabel)
        alabel = "a%d" % i
        filter_parts.append(
            "[%d:a]atrim=start=%.3f:duration=%.3f,asetpts=PTS-STARTPTS,"
            "aresample=22050,aformat=channel_layouts=mono[%s]" % (input_idx, t["trimStart"], dur, alabel))
        concat_labels.append(alabel)

    filter_complex = ";".join(filter_parts) + ";" + "".join("[%s]" % l for l in concat_labels) + \
        "concat=n=%d:v=0:a=1[out]" % len(concat_labels)
    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + [
        "-filter_complex", filter_complex, "-map", "[out]",
        "-ac", "1", "-ar", "22050", "-acodec", "pcm_s16le", out_path]
    p = run(cmd, timeout=300)
    if p.returncode != 0 or not os.path.exists(out_path):
        return "ffmpeg assembly failed: " + (p.stdout[-800:] if p.stdout else "")
    return None


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


def process_beat_requested(session_id, d):
    write_status(d, "processing")
    beat = load_beat(d)
    project = os.path.join(d, "project.json")
    p = run(BEATSTUDIO + ["init", "--budget=0", "--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message="init failed", detail=p.stdout[-800:])
        return
    p = generate_beat(project, beat)
    if p.returncode != 0:
        write_status(d, "error", message="beat generation failed", detail=p.stdout[-800:])
        return
    p = run(BEATSTUDIO + ["beat-render", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="beat render failed", detail=p.stdout[-800:])
        return
    p = run(BEATSTUDIO + ["preview", "--file=" + project], timeout=120)
    if p.returncode != 0:
        write_status(d, "error", message="beat preview failed", detail=p.stdout[-800:])
        return

    out_dir = os.path.join(RESULTS_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(os.path.join(d, "preview.wav"), os.path.join(out_dir, "beat_preview.wav"))
    os.chmod(os.path.join(out_dir, "beat_preview.wav"), 0o644)
    write_status(d, "beat_ready", beat_url="/beatstudio/results/" + session_id + "/beat_preview.wav",
                 genre=beat["genre"], bpm=beat["bpm"])


def process_uploaded(session_id, d):
    # Read whatever the "uploaded" status carried (legacy single-shot
    # uploads stash raw_ext there) BEFORE write_status()'s first call
    # below overwrites status.json wholesale - a real bug caught during
    # testing: write_status() replaces the whole payload (status +
    # updated_at + only whatever `extra` THIS call passes), so calling it
    # first silently erased raw_ext, and the legacy fallback path always
    # went looking for the wrong file extension afterward.
    status_path = os.path.join(d, "status.json")
    with open(status_path) as f:
        raw_ext = json.load(f).get("raw_ext", "webm")

    write_status(d, "processing")

    vocal_wav = os.path.join(d, "vocal_upload.wav")
    manifest_path = os.path.join(d, "manifest.json")
    fx = None
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        fx = manifest["fx"]
        err = assemble_takes(d, manifest, vocal_wav, "assemble")
        if err:
            write_status(d, "error", message="couldn't assemble your takes", detail=err)
            return
    else:
        # Legacy single-shot recording (no recording-workspace manifest) -
        # still supported so an in-flight session started against an
        # older page version doesn't just break mid-flow.
        raw_path = os.path.join(d, "vocal_raw." + raw_ext)
        if not os.path.exists(raw_path):
            write_status(d, "error", message="no uploaded audio found")
            return
        p = run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw_path, "-ac", "1", "-ar", "22050",
                 "-acodec", "pcm_s16le", vocal_wav])
        if p.returncode != 0 or not os.path.exists(vocal_wav):
            write_status(d, "error", message="couldn't decode the recording", detail=p.stdout[-800:] if p.stdout else "")
            return

    beat = load_beat(d)
    project = os.path.join(d, "project.json")
    p = run(BEATSTUDIO + ["init", "--budget=1000", "--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message="init failed", detail=p.stdout[-800:])
        return

    p = generate_beat(project, beat)
    if p.returncode != 0:
        write_status(d, "error", message="beat generation failed", detail=p.stdout[-800:])
        return

    p = run(BEATSTUDIO + ["beat-render", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="beat render failed", detail=p.stdout[-800:])
        return

    p = run(BEATSTUDIO + ["track-import", vocal_wav, "--name=vocal", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="couldn't add your recording as a track", detail=p.stdout[-800:])
        return

    if fx and (fx["fadeIn"] > 0 or fx["fadeOut"] > 0 or fx["autotrim"] or fx["gate"] or fx["deess"]):
        edit_flags = ["--fade-in=" + str(fx["fadeIn"]), "--fade-out=" + str(fx["fadeOut"])]
        if fx["autotrim"]:
            edit_flags.append("--autotrim")
        if fx["gate"]:
            edit_flags.append("--gate")
        if fx["deess"]:
            edit_flags.append("--deess")
        p = run(BEATSTUDIO + ["voice-edit", "vocal"] + edit_flags + ["--file=" + project], timeout=300)
        if p.returncode != 0:
            write_status(d, "error", message="voice editing failed", detail=p.stdout[-800:])
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
        if status not in ("uploaded", "rerender_requested", "beat_requested"):
            continue
        try:
            if status == "uploaded":
                process_uploaded(session_id, d)
            elif status == "rerender_requested":
                process_rerender(session_id, d)
            else:
                process_beat_requested(session_id, d)
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
