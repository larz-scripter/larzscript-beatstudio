# PLAN 2: recording workspace, auto beat maker, vocal editing, polish

Status: **drafted 2026-08-04, not yet built.** PLAN.md (the chunked-
streaming + interactive mixer rewrite) is shipped and live. This is the
next round, recorded the same way for the same reason - so a future
session can pick it up without re-deriving it.

## Why

After the interactive mixer shipped, the user asked two direct questions
that exposed real scope gaps:

1. **"can i play with the recorded voice"** - no. Today a vocal track only
   gets Volume + Pan, identical to the beat track. No trim, no fade, no
   noise handling.
2. **"is there the automatic beat maker"** - only underneath. The engine
   (`beatstudio.lz`) already has a real step sequencer (`step`/
   `pattern-add`/`pattern-step`/`arrange`), but the web page always uses
   ONE fixed hardcoded demo beat. Nothing on the page lets a visitor
   generate or vary it.

Then, mid-conversation, three more requirements landed:
- **"an area to work with the beat and voice so i can do recording in
  bits and arrange altogether"** - a real multi-take recording
  workspace, not one continuous take.
- **"the beat maker should be able to auto generate beat for me"** -
  one-click procedural generation, not just a preset picker.
- Make it **"very seamless... nice styling and UX"**.

The user picked the recommended phase order (Ops -> Recording workspace/
vocal editing -> Beat maker -> Polish) when asked, via `AskUserQuestion`.

## An operating gap found while checking for errors (do first, cheap)

Checked production for errors before planning anything new: **zero**
across all 4 real sessions since the interactive-mixer deploy, no OOM, no
Apache errors. But found a real gap - **nothing cleans up old session
files** (`/root/beatstudio_sessions/`, `/var/www/larzos/beatstudio/
results/`), and **beatstudio isn't in `/root/error_watch.py`'s
monitored-apps list** (`FILES`/`LOGCMD` dicts) - the estate's hourly
cross-app error watcher has no idea this app exists, so a real production
failure here would alert no one. Disk is already at 79% used (9.9GB
free) and 8-minute recordings are ~6x the old 30s cap's file size -
worth fixing before it's a real incident, not after.

## Phase A - ops hardening

- A daily cleanup cron (`/opt/beatstudio/cleanup.py`) that removes session
  dirs + result dirs older than a retention window (proposal: 7 days -
  long enough that someone revisiting their result the next day still
  finds it, short enough to bound disk growth). Delete both
  `/root/beatstudio_sessions/<id>/` and `/var/www/larzos/beatstudio/
  results/<id>/` together, keyed off `status.json`'s `updated_at`.
- Add beatstudio to `error_watch.py`'s `FILES` dict
  (`/var/log/beatstudio_process.log`), matching how every other app on
  the box is already monitored - a failed render should page the same
  way a failed CryptoLarz request does.

## Phase B - recording workspace (record in bits, arrange, edit)

**Architecture decision**: keep the "record in bits" feature as an
*upload-time assembly* step, not a new engine capability. The browser
records N separate clips; each clip gets a position (order + a gap-before
in seconds) and a trim range; the browser uploads all clips + a manifest;
the backend uses `ffmpeg` to assemble them into ONE continuous WAV
(silence-padded at the right offsets - `ffmpeg`'s `adelay`/`concat`/
`anullsrc`), then the EXISTING pipeline (`track-import` on that one
assembled file) runs completely unchanged. This avoids adding
per-track-offset complexity to `beatstudio.lz`'s mixer (today every track
starts at sample 0) and keeps the assembly step scoped to exactly what
`ffmpeg` is already disclosed for (format/container handling), not audio
processing.

**Vocal editing itself stays real Larzscript DSP, not ffmpeg filters** -
`ffmpeg`'s `agate`/`afade`/`silenceremove` filters exist and would be the
"quick" way to do this, but using them for actual signal processing would
quietly break the project's whole "every DSP stage is real, from-scratch
Larzscript" identity (the thing the READMEs go out of their way to be
honest about). Implement instead, extending the same native-buffer
pattern `_native_master_block` already established:
- **Trim**: pure sample-range selection when reading a clip - no new DSP.
- **Fade in/out**: a linear gain ramp over the first/last N samples - new
  small native builtin, `_native_fade_buffer(buf, start_gain, end_gain)`.
- **Auto-trim leading silence**: scan for the first sample crossing an
  RMS/peak threshold - small native builtin, reuses the peak-scan
  approach `_native_peak_abs` already established.
- **Noise gate**: an envelope-follower + threshold, same technique
  `compressor_gain()` already uses (attack/release smoothing) with an
  inverted transfer curve (attenuate BELOW threshold instead of above) -
  a real but small new DSP unit, direct sibling of the existing
  compressor.
- **De-esser**: a band-limited (5-8kHz) sidechain compressor - a
  band-pass filter feeding a compressor's level detector, gain-reducing
  only that band. Directly reuses the existing biquad + compressor
  building blocks, no new math primitive needed.
- **Explicitly NOT in this phase**: true spectral noise removal and
  pitch correction/autotune. Both need real FFT infrastructure that
  doesn't exist anywhere in this stack yet (checked - `dsp` has zero
  spectral/FFT code). That's a dedicated feasibility spike of its own,
  not a quick add alongside everything else here - flagged for a future
  round, not silently dropped.

**UX** (redesigning the "Record on it" section into a workspace):
- An ordered list of "take" cards, not one recording. Each card: a
  duration bar, trim-start/trim-end numeric controls, a gap-before
  (seconds) control, Preview/Re-record/Delete buttons, and up/down
  reorder buttons (not free drag-and-drop - more reliable to build and
  verify without a live browser to test dragging in).
- "+ Record another take" appends a new card instead of replacing the
  one recording, so a verse/chorus/verse structure can be built up
  performance by performance.
- **Client-side preview of the whole arrangement before uploading
  anything** - decode each clip via the Web Audio API and play them back
  with the configured gaps, entirely in the browser, no server round
  trip. This is the "seamless" part - hearing the arrangement together
  before committing to a render.
- Auto-trim silence / noise gate / de-esser as simple on/off toggles
  (strength sliders can come later once the on/off versions are proven
  out) applied to the assembled vocal track.
- "Use this arrangement" uploads clips + manifest and kicks off
  processing - same status-polling UX already built for the single-take
  flow, no new interaction pattern to learn.

## Phase C - a real, auto-generating beat maker

**Generation, not just presets**: a `generate_pattern(genre, energy, bpm,
seed)`-style function in `beatstudio.lz`, using the `random` package
(already a dependency via `dsp.noise()`), that builds a musically-
constrained 16-step pattern per voice - not pure randomness (a purely
random grid doesn't sound like a beat), but randomized *within* rules
per genre (e.g. kick-heavy on 1/9 with probabilistic extra hits for
"boom bap", four-on-the-floor for "house", syncopated/sparse for
"lo-fi"), plus a small hand-authored template library each genre's
generator riffs on/varies rather than starting from nothing. One-click
"Generate a new beat" is the primary interaction; the existing manual
step-grid becomes an "advanced/fine-tune" mode for anyone who wants to
hand-edit what got generated, not the default entry point.
- Tempo control (bpm slider) - already a real `beatstudio.lz` parameter
  (`init --bpm=`), just never exposed on the page.
- A handful more drum voices (open hihat, tom, ride/crash, 808 sub,
  extra percussion) - same synthesis pattern the existing 4 voices
  already use, no new architecture needed.
- Backend: `process.py`'s hardcoded `MAIN_STEPS`/`NAMED_STEPS`/
  `ARRANGEMENT` become real parameters (a `beat.json` alongside
  `params.json`), and beat-render gets re-run when they change - already
  fast thanks to the per-unique-pattern caching from the last round (a
  151-bar/5-minute arrangement rendered in ~9-13s).

## Phase D - professional polish

- **Sidechain ducking**: the beat's level dips automatically when the
  vocal is present (the "pumping" effect real vocal mixes use so the
  beat doesn't fight the voice) - a cross-track envelope follower in
  `mix_chunk` (read the vocal's level, derive a gain-reduction envelope,
  apply it to the beat before summing). Moderate engine change, direct
  extension of the compressor envelope-follower already built.
- **Stereo widening**: mid-side processing (`M=(L+R)/2, S=(L-R)/2`,
  scale S, recombine) - simple linear algebra, a small new native
  builtin.
- **Harmonic saturation**: soft-clip/waveshaping (tanh-style) for
  analog-style warmth/loudness perception - simple, no new primitive
  class needed.
- **A simple delay/reverb send for the vocal**: start with a single
  feedback delay line (an actual "echo," genuinely useful and much
  simpler than a full reverb); a proper algorithmic reverb (Schroeder/
  Freeverb-style comb+allpass network) is a good v2 if the delay alone
  isn't enough - both need a delay-line primitive that doesn't exist yet
  (a circular buffer + read/write, straightforward addition).

## Open questions for whoever picks this up

- Session retention window: proposed 7 days - confirm or adjust before
  building the cleanup cron.
- Genre list for the beat generator: needs a real first pass (proposed
  starting set: Trap, Lo-fi, Boom Bap, Pop, Afrobeat, House) - subject to
  revision once a couple are actually generated and listened to.
- Whether reorder-by-buttons is an acceptable substitute for drag-and-
  drop in the recording workspace, or worth a follow-up pass with real
  browser testing (this session still has no browser/Node available -
  same caveat as the last plan).
