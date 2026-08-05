"""beatstudio processing worker - run via cron every minute.

Job types, distinguished by session status:
  "beat_requested"      -> free, no wallet involved: generate + beat-render
                           + preview just the beat (beat.json's genre/bpm/
                           bars), so a visitor can hear/regenerate it BEFORE
                           committing to record - PLAN2.md Phase C.
  "uploaded"             -> free pipeline: assemble takes (if a recording-
                           workspace manifest.json is present) or decode the
                           legacy single upload -> (first time only) init
                           project + generate the beat -> track-import vocal
                           -> voice-edit (fade/autotrim/gate/de-ess, if
                           requested) -> autotune/double/harmonize (if
                           requested) -> mix (default levels) -> preview.
                           Stops at "preview_ready" - NO master, nothing
                           charged yet. A visitor lands back here from
                           "Use this arrangement" every time, including a
                           RETAKE (re-recording/re-trimming after already
                           hearing a preview): the project already has its
                           beat/melody tracks rendered, so a retake skips
                           straight to track-import onward - see the
                           project.json existence check below - PLAN6.md
                           Phase Q.
  "finalize_requested"  -> triggered by a visitor who liked what they heard
                           at "preview_ready" and wants to commit: mix ->
                           preview -> master (PAID the first time only,
                           FREE after that per beatstudio.lz's own
                           master/remaster split) -> publish. PLAN6.md Phase Q.
  "rerender_requested"  -> lighter job, for AFTER a master already exists:
                           mix (new gain/pan/mute) -> preview -> remaster
                           (FREE) with new EQ/compressor/loudness settings.
                           Reuses the beat/vocal tracks already rendered -
                           no re-decode, no re-synthesis.
All drop results where Apache serves them as static files.
"""
import copy
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
DEFAULT_BEAT = {"genre": "boombap", "bpm": 140.0, "bars": 4, "seed": 1, "melody": True, "targetSeconds": 0}

DEFAULT_STRIP = {"eqLow": 0.0, "eqMid": 0.0, "eqHigh": 0.0, "compThresh": 0.0, "compRatio": 3.0,
                  "duckFrom": None, "delayMs": 0.0, "delayFeedback": 0.3, "delayMix": 0.0,
                  "reverbMix": 0.0, "reverbFeedback": 0.4}

DEFAULT_PARAMS = {
    "tracks": {
        "beat": {"gain": -1.0, "pan": 0.0, "mute": False, "strip": DEFAULT_STRIP},
        "vocal": {"gain": 2.0, "pan": 0.0, "mute": False, "strip": DEFAULT_STRIP},
        "melody": {"gain": -6.0, "pan": 0.0, "mute": False, "strip": DEFAULT_STRIP},
    },
    "master": {"preset": None, "low": 2.0, "mid": 0.5, "high": 3.0, "thresh": -14.0, "ratio": 3.0,
               "ceiling": -1.0, "width": 1.0, "saturation": 0.0, "targetLufs": None},
}

# The full set of track names this pipeline knows about - not every
# project has all three (a drums-only beat has no "melody" track; a
# beat-only preview has no "vocal" yet), so every place that iterates
# this list must tolerate a missing track rather than assume it exists.
TRACK_NAMES = ("beat", "vocal", "melody")

# Layered vocal tracks that only exist when the matching fx flag is on
# (see beatstudio.lz's `double`/`harmonize` - each writes/upserts a FIXED
# track name, so re-running with the flag on again just replaces it).
# The real hazard this maps for: a RETAKE (PLAN6.md Phase Q) can turn a
# flag OFF that was ON for a prior take - "vocal_double" would otherwise
# keep sitting in project.json with the OLD take's audio and mix_chunk
# includes every non-muted track regardless of what apply_mix touches, so
# it would keep playing a stale layer nobody asked for anymore. Muted
# explicitly below rather than left alone whenever the flag is off but
# the track still exists from an earlier take.
EXTRA_VOCAL_TRACKS = {"vocal_double": "double", "vocal_harmony": "harmonize"}

MASTER_PRICE = "2"


def write_params(d, params):
    tmp = os.path.join(d, "params.json.tmp")
    with open(tmp, "w") as f:
        json.dump(params, f)
    os.replace(tmp, os.path.join(d, "params.json"))


def mute_stale_extra_tracks(project, fx, existing, log_prefix, d):
    """See EXTRA_VOCAL_TRACKS above - only matters on a retake where a
    layer that WAS requested no longer is. A no-op (0 calls) on a normal
    first-time upload, since a track named "vocal_double"/"vocal_harmony"
    can only exist here if a PRIOR pass on this same project created it."""
    for name, flag in EXTRA_VOCAL_TRACKS.items():
        if name not in existing:
            continue
        if fx and fx.get(flag):
            continue
        p = run(BEATSTUDIO + ["mix", name, "--mute=true", "--file=" + project])
        if p.returncode != 0:
            write_status(d, "error", message=log_prefix + ": couldn't mute stale '" + name + "' from an earlier take", detail=p.stdout[-800:])
            return p
    return None


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
                "bars": b.get("bars", DEFAULT_BEAT["bars"]), "seed": b.get("seed", DEFAULT_BEAT["seed"]),
                "melody": b.get("melody", DEFAULT_BEAT["melody"]),
                "targetSeconds": b.get("targetSeconds", DEFAULT_BEAT["targetSeconds"])}
    except (json.JSONDecodeError, OSError):
        return DEFAULT_BEAT


def generate_beat(project, beat, timeout=180):
    cmd = BEATSTUDIO + ["generate", beat["genre"], "--bpm=" + str(beat["bpm"]),
                         "--bars=" + str(beat["bars"]), "--seed=" + str(beat["seed"]),
                         "--file=" + project]
    if not beat.get("melody", True):
        cmd.append("--no-melody")
    # PLAN4.md Phase I: a real verse/chorus/bridge structure scaled to
    # this many seconds instead of the short fixed template - 0 (default)
    # keeps the existing short behavior exactly as before.
    if beat.get("targetSeconds", 0) > 0:
        cmd.append("--target-seconds=" + str(beat["targetSeconds"]))
    return run(cmd, timeout=timeout)


def melody_render(project, timeout=300):
    """Always safe to call - melody-render itself checks the project's
    melody_enabled flag and just mutes/no-ops if it's off (see
    beatstudio.lz's own cmd_melody_render), so this doesn't need its own
    conditional here."""
    return run(BEATSTUDIO + ["melody-render", "--file=" + project], timeout=timeout)


def project_track_names(project):
    """Which tracks actually exist on this project right now - a
    drums-only beat has no "melody" track, a beat-only preview has no
    "vocal" yet. Every per-track loop below (mix/strip) needs to know
    this so it can skip a track beatstudio.lz would otherwise reject with
    "no track named 'X'" instead of treating that as a fatal pipeline
    error."""
    try:
        with open(project) as f:
            p = json.load(f)
        return set(t["name"] for t in p.get("tracks", []))
    except (json.JSONDecodeError, OSError, KeyError):
        return set()


def read_track_gain(project, name):
    """Reads back a track's CURRENT gain_db - used right after
    track-import --auto-gain so the initial apply_mix call (below) can
    preserve that real, measured gain-staging value instead of
    overwriting it with DEFAULT_PARAMS' fixed default."""
    try:
        with open(project) as f:
            p = json.load(f)
        for t in p.get("tracks", []):
            if t["name"] == name:
                return t.get("gain_db")
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def read_track_compressor(project, name):
    """Reads back a track's auto-set comp_thresh_db/comp_ratio, if
    track-import --auto-gain engaged real vocal leveling on a recording
    with an unusually large peak-to-RMS gap (see beatstudio.lz's own
    comment on that). apply_strip below always sends --clear before
    reapplying a track's strip params from `params` (a track with no
    manual EQ/compression configured produces the exact same
    beatstudio.lz call every time) - without reading this back first,
    that --clear would silently wipe the auto-leveling compressor the
    moment apply_mix runs, right after track-import sets it."""
    try:
        with open(project) as f:
            p = json.load(f)
        for t in p.get("tracks", []):
            if t["name"] == name and t.get("comp_thresh_db") is not None:
                return t["comp_thresh_db"], t.get("comp_ratio", 3.0)
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def project_is_budgeted(project):
    """True only for a project that went through the REAL `init
    --budget=1000` process_uploaded runs - never true for the FREE
    beat-only preview project process_beat_requested creates (`init
    --budget=0`, PLAN2.md Phase C's "Generate a new beat" button).

    A real regression this fixed: both write to the exact same
    d/project.json path. PLAN6.md Phase Q's retake detection originally
    checked plain file EXISTENCE, which made the (very common - "Choose
    your beat" is the first thing on the page) case of a visitor
    previewing a beat before ever recording look identical to a retake:
    process_uploaded would skip its own init call and silently keep the
    beat-preview project's budget_cents=0 forever, so the LATER paid
    master step failed outright ("can't afford a professional master,
    $2.00 needed, $0.00 remaining") for every such visitor - caught live
    via a real user's error report, not in testing."""
    try:
        with open(project) as f:
            return json.load(f).get("budget_cents", 0) > 0
    except (json.JSONDecodeError, OSError):
        return False


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


def apply_strip(project, name, strip, existing, log_prefix, d):
    """Sets one track's channel strip (PLAN3.md Phase F/G) - EQ,
    compressor, sidechain duck-from, delay/reverb send. Always sends
    --clear first so a strip that WAS configured and got reset back to
    "off" in the UI actually clears on the track too, not just skips
    setting new values on top of stale ones. `existing` is this
    project's actual current track set (see project_track_names) -
    beatstudio.lz's `strip --duck-from` fails closed if the named track
    doesn't exist (e.g. a stale UI selection pointing at "melody" on a
    drums-only project), so that flag is dropped rather than sent when
    the target track isn't actually there."""
    flags = ["--clear"]
    if strip["eqLow"] != 0.0: flags.append("--eq-low=" + str(strip["eqLow"]))
    if strip["eqMid"] != 0.0: flags.append("--eq-mid=" + str(strip["eqMid"]))
    if strip["eqHigh"] != 0.0: flags.append("--eq-high=" + str(strip["eqHigh"]))
    if strip["compThresh"] != 0.0:
        flags.append("--comp-thresh=" + str(strip["compThresh"]))
        flags.append("--comp-ratio=" + str(strip["compRatio"]))
    if strip["duckFrom"] and strip["duckFrom"] in existing and strip["duckFrom"] != name:
        flags.append("--duck-from=" + strip["duckFrom"])
    if strip["delayMs"] > 0.0 and strip["delayMix"] > 0.0:
        flags.append("--delay-ms=" + str(strip["delayMs"]))
        flags.append("--delay-feedback=" + str(strip["delayFeedback"]))
        flags.append("--delay-mix=" + str(strip["delayMix"]))
    if strip.get("reverbMix", 0.0) > 0.0:
        flags.append("--reverb-mix=" + str(strip["reverbMix"]))
        flags.append("--reverb-feedback=" + str(strip.get("reverbFeedback", 0.4)))
    p = run(BEATSTUDIO + ["strip", name] + flags + ["--file=" + project])
    if p.returncode != 0:
        write_status(d, "error", message=log_prefix + ": couldn't set '" + name + "' channel strip", detail=p.stdout[-800:])
        return p
    return None


def apply_mix(project, params, log_prefix, d):
    """Sets every EXISTING track's gain/pan/mute + channel strip from
    params (a drums-only project has no "melody" track, a beat-only
    preview has no "vocal" yet - see project_track_names). Returns the
    failed CompletedProcess on error, or None on success - callers
    write_status and return themselves so a partial mix state never gets
    reported as "done"."""
    existing = project_track_names(project)
    for name in TRACK_NAMES:
        if name not in existing:
            continue
        t = params["tracks"].get(name, DEFAULT_PARAMS["tracks"].get(name, {"gain": 0.0, "pan": 0.0, "mute": False, "strip": DEFAULT_STRIP}))
        mute_flag = "--mute=true" if t["mute"] else "--unmute"
        p = run(BEATSTUDIO + ["mix", name, "--gain=" + str(t["gain"]), "--pan=" + str(t["pan"]),
                               mute_flag, "--file=" + project])
        if p.returncode != 0:
            write_status(d, "error", message=log_prefix + ": couldn't set '" + name + "' mix", detail=p.stdout[-800:])
            return p
        err = apply_strip(project, name, t.get("strip", DEFAULT_STRIP), existing, log_prefix, d)
        if err is not None:
            return err
    return None


def render_preview_and_master(project, params, d, is_first_master):
    write_status(d, "processing", stage="Mixing your preview...")
    p = run(BEATSTUDIO + ["preview", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="preview mix failed", detail=p.stdout[-800:])
        return False

    m = params["master"]
    # --preset first (a real starting point, see beatstudio.lz's own
    # mastering_preset()) - the explicit low/mid/high/etc. flags after it
    # still win field-by-field, same override order the CLI itself uses.
    master_flags = []
    if m.get("preset"):
        master_flags.append("--preset=" + m["preset"])
    master_flags += ["--low=" + str(m["low"]), "--mid=" + str(m["mid"]), "--high=" + str(m["high"]),
                      "--thresh=" + str(m["thresh"]), "--ratio=" + str(m["ratio"]), "--ceiling=" + str(m["ceiling"]),
                      "--width=" + str(m.get("width", 1.0)), "--saturation=" + str(m.get("saturation", 0.0))]
    # PLAN5.md Phase N - only sent when a target was actually picked, so
    # a project that never asks for it doesn't pay the measurement
    # pre-pass beatstudio.lz's own --target-lufs implementation costs.
    if m.get("targetLufs"):
        master_flags.append("--target-lufs=" + str(m["targetLufs"]))
    if is_first_master:
        cmd = BEATSTUDIO + ["master", "--price=" + MASTER_PRICE] + master_flags + ["--file=" + project]
        write_status(d, "processing", stage="Mastering your track...")
    else:
        cmd = BEATSTUDIO + ["remaster"] + master_flags + ["--file=" + project]
        write_status(d, "processing", stage="Re-mastering with your new settings...")
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


def publish_preview(project, d, session_id):
    """PLAN6.md Phase Q - the free, pre-master checkpoint: publishes the
    mix-only preview (no EQ/compressor/limiter, no charge) so a visitor
    can hear the vocal WITH the beat before deciding whether to retake or
    commit to mastering. A distinct filename from before.wav/after.wav
    (publish_results below) - the two never coexist in meaning (this one
    is superseded the moment a real master exists) but keeping them
    separate avoids a stale post-master "before" being momentarily
    confused with the un-mastered take a visitor is currently reviewing
    on a retake."""
    out_dir = os.path.join(RESULTS_DIR, session_id)
    os.makedirs(out_dir, exist_ok=True)
    shutil.copyfile(os.path.join(d, "preview.wav"), os.path.join(out_dir, "preview_take.wav"))
    os.chmod(os.path.join(out_dir, "preview_take.wav"), 0o644)
    track_names = project_track_names(project)
    write_status(d, "preview_ready", preview_url="/beatstudio/results/" + session_id + "/preview_take.wav",
                 melody="melody" in track_names)


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

    # PLAN5.md Phase O - real stem export: every track that actually got
    # rendered onto this project (beat/vocal/melody plus any layered
    # extras like vocal_double/vocal_harmony), published individually so
    # a visitor can pull the separated parts for remixing elsewhere, not
    # just the final 2-track mix. Best-effort per file - one missing/
    # unreadable track file shouldn't take down the whole "done" result,
    # since the mix/master already succeeded without needing it re-read.
    stems = {}
    for t in proj.get("tracks", []):
        src = t.get("path")
        if not src or not os.path.exists(src):
            continue
        stem_name = "stem_" + t["name"] + ".wav"
        try:
            shutil.copyfile(src, os.path.join(out_dir, stem_name))
            os.chmod(os.path.join(out_dir, stem_name), 0o644)
            stems[t["name"]] = "/beatstudio/results/" + session_id + "/" + stem_name
        except OSError:
            pass

    # Real track presence, not the beat.json request flag - the source of
    # truth the mixer UI needs for "should the Melody panel be shown at
    # all" is whatever actually got rendered onto this project, not what
    # was originally asked for.
    track_names = set(t["name"] for t in proj.get("tracks", []))
    write_status(d, "done", before_url="/beatstudio/results/" + session_id + "/before.wav",
                 after_url="/beatstudio/results/" + session_id + "/after.wav",
                 peak_dbfs=masters[-1]["peak_dbfs"], melody="melody" in track_names,
                 lufs=masters[-1].get("lufs"), stems=stems)


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
    if beat.get("melody", True):
        p = melody_render(project)
        if p.returncode != 0:
            write_status(d, "error", message="melody render failed", detail=p.stdout[-800:])
            return
        # Default melody level for the FREE beat-only preview - matches
        # DEFAULT_PARAMS["tracks"]["melody"]["gain"] used once a vocal is
        # recorded, so the preview a visitor hears here doesn't jump in
        # relative balance the moment they move on to recording.
        p = run(BEATSTUDIO + ["mix", "melody", "--gain=-6.0", "--file=" + project])
        if p.returncode != 0:
            write_status(d, "error", message="couldn't set melody level", detail=p.stdout[-800:])
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
                 genre=beat["genre"], bpm=beat["bpm"], melody=beat.get("melody", True))


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

    write_status(d, "processing", stage="Getting started...")

    vocal_wav = os.path.join(d, "vocal_upload.wav")
    manifest_path = os.path.join(d, "manifest.json")
    fx = None
    if os.path.exists(manifest_path):
        with open(manifest_path) as f:
            manifest = json.load(f)
        fx = manifest["fx"]
        write_status(d, "processing", stage="Assembling your takes...")
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
    # PLAN6.md Phase Q: a project.json that's already been through the
    # REAL (budgeted) init means this is a RETAKE landing back here after
    # a "preview_ready" checkpoint (see process_finalize/publish_preview
    # below) - the beat/melody are already generated and rendered, so
    # skip straight to re-importing the vocal. Only a genuinely first-time
    # upload runs init/generate/render. Checking budget rather than plain
    # file existence on purpose - see project_is_budgeted's own comment
    # for the real regression that distinction fixes (a free beat-only
    # preview project sharing this exact same file path).
    is_retake = project_is_budgeted(project)
    if not is_retake:
        p = run(BEATSTUDIO + ["init", "--budget=1000", "--file=" + project])
        if p.returncode != 0:
            write_status(d, "error", message="init failed", detail=p.stdout[-800:])
            return

        write_status(d, "processing", stage="Generating your beat...")
        p = generate_beat(project, beat)
        if p.returncode != 0:
            write_status(d, "error", message="beat generation failed", detail=p.stdout[-800:])
            return

        write_status(d, "processing", stage="Rendering the beat...")
        p = run(BEATSTUDIO + ["beat-render", "--file=" + project], timeout=300)
        if p.returncode != 0:
            write_status(d, "error", message="beat render failed", detail=p.stdout[-800:])
            return

        if beat.get("melody", True):
            write_status(d, "processing", stage="Rendering bass & chords...")
            p = melody_render(project)
            if p.returncode != 0:
                write_status(d, "error", message="melody render failed", detail=p.stdout[-800:])
                return
    else:
        write_status(d, "processing", stage="Preparing your retake...")

    write_status(d, "processing", stage="Adding your recording...")
    # --auto-gain: real gain-staging on import, not a cosmetic default -
    # see beatstudio.lz's own comment on the production incident this
    # fixes (a mic recording so quiet even max manual gain couldn't
    # rescue it once mixed).
    p = run(BEATSTUDIO + ["track-import", vocal_wav, "--name=vocal", "--auto-gain", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="couldn't add your recording as a track", detail=p.stdout[-800:])
        return

    if fx and (fx["fadeIn"] > 0 or fx["fadeOut"] > 0 or fx["autotrim"] or fx["gate"] or fx["deess"]):
        write_status(d, "processing", stage="Cleaning up your vocal...")
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

    # PLAN4.md Phase J real pitch correction - runs on the CLEANED vocal
    # (after voice-edit above), and BEFORE double/harmonize below so a
    # harmony layer is built from the already-corrected pitch, not the
    # raw one. strength=0 (default) is a true no-op on the beatstudio.lz
    # side, so this call is always safe to make unconditionally when a
    # manifest exists - matches apply_strip's own "always send, let the
    # engine no-op" convention above.
    if fx and fx.get("autotuneStrength", 0.0) > 0.0:
        write_status(d, "processing", stage="Correcting pitch...")
        at_flags = ["--strength=" + str(fx["autotuneStrength"]), "--key=" + str(fx.get("autotuneKey", 0.0))]
        if fx.get("autotuneScale"):
            at_flags.append("--scale=" + fx["autotuneScale"])
        p = run(BEATSTUDIO + ["autotune", "vocal"] + at_flags + ["--file=" + project], timeout=300)
        if p.returncode != 0:
            write_status(d, "error", message="pitch correction failed", detail=p.stdout[-800:])
            return

    # PLAN3.md Phase G quick double/harmony - runs on the CLEANED (and now
    # possibly pitch-corrected) vocal, so the doubled layer gets the same
    # benefit the main vocal does rather than doubling the raw recording.
    if fx and fx.get("double"):
        write_status(d, "processing", stage="Adding a double...")
        p = run(BEATSTUDIO + ["double", "vocal", "--semitones=" + str(fx.get("doubleSemitones", 0.15)),
                               "--file=" + project], timeout=180)
        if p.returncode != 0:
            write_status(d, "error", message="couldn't create the vocal double", detail=p.stdout[-800:])
            return

    # PLAN5.md Phase M chord-aware harmony - a separate, optional layer
    # from `double` above (a visitor could conceivably want both: a
    # subtle double AND a real harmony line). Same "run after pitch
    # correction" ordering reasoning.
    if fx and fx.get("harmonize"):
        write_status(d, "processing", stage="Adding harmony...")
        p = run(BEATSTUDIO + ["harmonize", "vocal", "--interval=" + fx.get("harmonizeInterval", "third"),
                               "--file=" + project], timeout=180)
        if p.returncode != 0:
            write_status(d, "error", message="couldn't create the harmony - does this project have a chord progression? (drums-only beats can't harmonize)", detail=p.stdout[-800:])
            return

    # See EXTRA_VOCAL_TRACKS's own comment - only ever mutes something on a
    # retake that turned a previously-on double/harmonize flag off.
    if mute_stale_extra_tracks(project, fx, project_track_names(project), "cleanup", d) is not None:
        return

    # A real deepcopy, not a reference - DEFAULT_PARAMS is a module-level
    # dict reused across every session; mutating it directly would leak
    # one session's auto-gain value into every other session's defaults.
    params = copy.deepcopy(DEFAULT_PARAMS)
    vocal_gain = read_track_gain(project, "vocal")
    if vocal_gain is not None:
        params["tracks"]["vocal"]["gain"] = vocal_gain
    vocal_comp = read_track_compressor(project, "vocal")
    if vocal_comp is not None:
        params["tracks"]["vocal"]["strip"]["compThresh"] = vocal_comp[0]
        params["tracks"]["vocal"]["strip"]["compRatio"] = vocal_comp[1]
    write_status(d, "processing", stage="Setting mix levels...")
    if apply_mix(project, params, "initial mix", d) is not None:
        return
    # PLAN6.md Phase Q: stop at the free preview checkpoint - no master,
    # nothing charged. Persist the params used so process_finalize (once
    # a visitor actually commits) mixes with the exact same levels the
    # preview they approved was built from, rather than silently
    # reconstructing DEFAULT_PARAMS and losing the auto-gain value.
    write_params(d, params)
    write_status(d, "processing", stage="Mixing your preview...")
    p = run(BEATSTUDIO + ["preview", "--file=" + project], timeout=300)
    if p.returncode != 0:
        write_status(d, "error", message="preview mix failed", detail=p.stdout[-800:])
        return
    publish_preview(project, d, session_id)


def process_finalize(session_id, d):
    """PLAN6.md Phase Q: triggered once a visitor has heard the free
    preview (and retaken/adjusted as many times as they wanted) and hits
    "Mix & master" - the ONLY step that ever charges the wallet, and even
    then only the first time per project (beatstudio.lz's own
    master/remaster split, unchanged)."""
    project = os.path.join(d, "project.json")
    if not os.path.exists(project):
        write_status(d, "error", message="no project to master - record something first")
        return
    write_status(d, "processing", stage="Setting mix levels...")
    params = load_params(d)
    if apply_mix(project, params, "finalize", d) is not None:
        return
    with open(project) as f:
        is_first_master = len(json.load(f).get("masters", [])) == 0
    if not render_preview_and_master(project, params, d, is_first_master=is_first_master):
        return
    write_status(d, "processing", stage="Finishing up...")
    publish_results(project, d, session_id)


def process_rerender(session_id, d):
    project = os.path.join(d, "project.json")
    if not os.path.exists(project):
        write_status(d, "error", message="no project to remix - record something first")
        return
    write_status(d, "processing", stage="Applying your changes...")
    params = load_params(d)
    if apply_mix(project, params, "remix", d) is not None:
        return
    if not render_preview_and_master(project, params, d, is_first_master=False):
        return
    write_status(d, "processing", stage="Finishing up...")
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
        if status not in ("uploaded", "finalize_requested", "rerender_requested", "beat_requested"):
            continue
        try:
            if status == "uploaded":
                process_uploaded(session_id, d)
            elif status == "finalize_requested":
                process_finalize(session_id, d)
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
