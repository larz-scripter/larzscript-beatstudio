# PLAN: interactive mixer/master + long-form audio on larzos.com/beatstudio/

Status: **SHIPPED 2026-08-04.** Drafted 2026-08-03, all six phases built,
verified, and deployed live the same session. Left in place (updated with
real final numbers instead of estimates) as the record of what was built
and why, per the same "don't lose this if the session ends" reasoning that
motivated writing it before any code existed.

## Why

The live page (`larzos.com/beatstudio/`) today is a fixed one-shot pipeline:
a hardcoded 140bpm demo beat, a 30-second vocal recording cap, hardcoded mix
gains (`beat -1dB / vocal +2dB`) and hardcoded master EQ (`--low=2 --mid=0.5
--high=3`), with a single before/after A-B compare as the only feedback. The
visitor never touches a control. Two things changed that:

1. The user wants to actually **mix and master it themselves** on the page —
   real controls, not a black box — so "anybody" can walk out with a
   professional-sounding result, not just a demo.
2. Recordings need to run to **5 minutes or more**, not 30 seconds. That
   collides directly with a real, already-hit constraint: `master_chain`
   OOM-killed on srv66 (1.9GB RAM) at just 60 seconds before it was rewritten
   to mutate one buffer in place instead of building 4-5 full-length copies
   (see `beatstudio.lz`'s `master_chain` comment + `[[project_larzscript_growth_roadmap]]`
   memory). Even in-place, a whole-buffer approach holds the *entire* track
   in RAM at once — at 5 minutes stereo/22.05kHz that's ~13.2M interleaved
   samples per buffer, times however many tracks are loaded simultaneously
   in `mix_to_stereo`. That scales linearly with length with no ceiling
   fix; it will hit the same wall again, just later.

Decisions already made (chosen by the user over the alternatives that were
also offered):

- **Control depth: both "real knobs, friendly labels" AND "full pro
  mixer."** Concretely: a real multi-track mixer (beat + vocal + any
  imported stems) with gain/pan/mute/solo per track, *and* the actual
  mastering chain (3-band EQ, compressor, limiter, loudness target) exposed
  as controls — but labeled in plain language a non-engineer understands
  (e.g. "Warmth" for the low shelf, "Loudness" for the normalize target),
  with the real unit (dB/Hz/ratio) shown alongside, not hidden. Every
  control gets a doc page explaining what it does and why, not just a
  tooltip.
- **5-minute scaling: rewrite to stream in fixed-size chunks** rather than
  move the render to different hardware or resize srv66. Must also come
  out **fast** — the user explicitly authorized dropping out of pure
  Larzscript for the hot DSP loop and compiling/binding it as C if the
  interpreted path isn't fast enough, the same "hosted-only, honestly
  documented" pattern the codebase already uses for `arecord` (mic capture)
  and `crypto` (X25519). This is not a last resort to reach for casually —
  Phase 0 below exists specifically to measure whether it's actually
  needed before adding that complexity.

## Current architecture (for context)

- `beatstudio.lz` (pure Larzscript, `dsp` + `wav` stack packages) — CLI:
  `init / step / pattern-add / pattern-step / arrange / beat-render / record
  / track-import / mix / preview / master / report`. State lives in
  `project.json` per session (patterns, tracks with gain/pan/mute, budget).
- `web/backend.py` (stdlib HTTP, :8478) — accepts an uploaded vocal blob per
  session id, writes `status.json`.
- `web/process.py` (cron, 1/minute, flock-serialized) — decodes the upload
  via `ffmpeg`, then shells out to `beatstudio.lz` with **hardcoded**
  bpm/pattern/arrangement/mix-gains/EQ values, copies `preview.wav` /
  `master_1.wav` to `/var/www/larzos/beatstudio/results/<session>/` as
  static files Apache serves.
- `web/page.html` — demo A-B player, record button (30s cap,
  `getUserMedia`/`MediaRecorder`), polls `/api/status` every 3s, shows a
  second A-B player for the result. No parameter controls exist client-side
  at all today — everything process.py does is fixed.

## Phase 0 — feasibility spikes (do first, before committing to any of the below)

Every past build in this ecosystem that skipped this step got burned (see
`[[project_larzscript_growth_roadmap]]`: the dict-literal interpreter bug,
the silent short-track-doesn't-loop bug, the hard-clipping bug, the OOM —
all found *after* shipping because something was assumed instead of
measured). Do not repeat that here at 10x the length and a real UI surface.

1. **Confirm the file-I/O primitives streaming needs actually exist** in
   the *currently deployed* native interpreter (v1.26.0 — my local
   `~/larzscript` checkout is stale relative to it, confirmed while
   researching this plan, so verify against `larzscript --version` on
   srv66 or a fresh `larzscript update`, not the local repo):
   - Chunked/ranged binary reads (`read_file_bytes` with an offset+length,
     or an explicit file-handle API) so `wav` doesn't have to load an
     entire multi-minute file into one byte-list just to stream it back
     out in blocks.
   - A way to patch the WAV header's `data`/`RIFF` size fields *after*
     streaming the body out (the sizes aren't known until the last chunk
     is written) — either an in-place byte-range write, or a two-pass
     scheme (write body via `append_file` first, then prepend/rewrite a
     correctly-sized header, verified to still produce a valid RIFF file).
   - `append_file`'s actual byte-safety on the deployed version (older
     local code only supports string content via `fputs`; production
     apparently supports binary today per the "binary-safe byte-list
     contract" note on `write_file` in `[[project_larzscript_growth_roadmap]]` —
     confirm `append_file` has the same contract, don't assume it inherited
     the fix).
   If any of these are missing, that's a real Larzscript interpreter
   feature gap — file it upstream (same as issue #4 was), and pick a
   fallback (e.g. writing numbered `.chunk` files and `cat`-equivalent
   concatenating at the end) rather than blocking on a language change.

2. **Benchmark the actual chunked hot loop** (biquad EQ x3, linked
   compressor, limiter, per-sample) at realistic length on the *sandbox*,
   then sanity-check against real hardware/CI the same way the original
   BeatStudio build did (this sandbox benchmarks ~4-5x slower than a
   GitHub Actions runner — don't extrapolate "too slow" from sandbox
   numbers alone, per `[[project_larzscript_growth_roadmap]]`). Get a real
   samples/sec figure for the interpreted path. Only if that number implies
   a 5-minute render would take an unreasonable wall-clock time (define
   "unreasonable" concretely before measuring — proposal: over ~3 minutes
   of processing for 5 minutes of audio) does the AOT/C path in Phase 2b
   become justified. If interpreted chunked processing is fast enough,
   skip Phase 2b entirely — simpler is better, the user authorized C as a
   fallback, not a default.
   - Also benchmark `larzscript compile` (the existing AOT-to-C toolchain
     already in `native/`) on the same hot loop before writing any new C —
     it may already deliver the needed speedup with zero new interpreter
     code, which is a much smaller lift than adding a new native builtin.

3. **Empirically calibrate the real memory ceiling on srv66** the same way
   the 50s number was calibrated (via `dmesg`, actually OOM-killing test
   renders on purpose, not estimating from element counts). Do this with
   the chunked implementation once Phase 1 exists, at increasing lengths
   (1 / 2 / 5 / 8 minutes) until it breaks or a sane upper bound is
   confirmed. Whatever that number is becomes the page's real, honest cap
   — "5 minutes or more" is the target, not a guarantee independent of
   what the hardware can actually do.

## Phase 1 — streaming core

- Add chunked read/write to the `wav` stack package (new functions
  alongside the existing whole-file `read`/`write`, which stay for
  short-clip callers): open a reader that yields fixed-size sample blocks
  (e.g. 1-2 seconds' worth) until exhausted; open a writer that accepts
  successive blocks and finalizes the header on close.
- Rework `mix_to_stereo` and `master_chain` in `beatstudio.lz` to operate
  block-by-block: pull one block from every input track's reader (looping
  the shorter ones exactly as today, just per-block instead of
  whole-buffer), mix, run through the EQ/compressor/limiter (all of which
  are already per-sample-stateful — biquad `z1/z2`, compressor envelope —
  so state just needs to persist across block boundaries instead of across
  buffer positions, which is a small change, not a redesign of the DSP
  itself), normalize (needs a first pass to find the true peak across all
  blocks before a second pass can scale — either buffer peak-so-far and do
  a cheap final rescale pass, or accept a slightly conservative fixed
  ceiling and skip true normalize-to-peak for streamed renders; decide
  after Phase 0 measures how expensive a second read-through actually is).
- **Verification, not "it didn't error":** render a short clip both ways
  (old whole-buffer path vs new chunked path) and diff/compare sample-by-
  sample — chunked must be bit-identical or inaudibly close, not just
  "sounds fine." Then specifically listen for/measure clicks or
  discontinuities *at chunk boundaries* (a common streaming-DSP bug class
  this codebase hasn't hit before) via the same FFT/level-analysis
  discipline used last time, per `[[project_larzscript_growth_roadmap]]`'s
  "verification discipline that changed mid-session" note.

## Phase 2 — make it fast enough

- 2a: ship with the chunked interpreted path if Phase 0's benchmark says
  it's fast enough (see the "unreasonable" threshold above).
- 2b (conditional): if not, compile the hot inner loop via `larzscript
  compile`'s existing AOT path first. Only if *that* still isn't enough,
  write a small native C builtin for the hot loop specifically (the
  per-sample EQ→compressor→limiter chain, not the whole app) and expose it
  from `beatstudio.lz`, documented with the same explicit honesty as the
  `arecord`/`crypto` hosted-only disclaimers already in this file's header
  comment — this is a deliberate, disclosed exception to "100% pure
  Larzscript DSP," not a quiet regression of that claim.

## Phase 3 — the actual mixer/master UI

- **Track mixer**: beat track + vocal track + (new) ability to import
  additional stems, each row with gain (dB slider), pan (L/R slider),
  mute, solo. Backed by `beatstudio.lz`'s existing `mix` command/`tracks`
  model — already supports per-track gain/pan/mute, just needs `solo` and
  a way for the web layer to set them per-track instead of hardcoding two
  `mix` calls in `process.py`.
- **Master controls**, each with a friendly label, the real unit next to
  it, and a linked (?) that opens the matching doc-page section:
  - "Warmth" — low shelf dB (`master --low`)
  - "Clarity" — mid peak dB (`master --mid`)
  - "Air" — high shelf dB (`master --high`)
  - "Punch" — compressor threshold/ratio (currently fixed at -14dB/3:1 in
    `master_chain`; needs to become a parameter)
  - "Loudness" — the normalize target (currently fixed -1.0dBFS; needs to
    become a parameter, with a sane safe range so nobody can push it into
    audible clipping)
- **Re-render model**: this is server-side real DSP, not a Web Audio
  live-preview — be upfront about that in the UI (an "Apply & re-render"
  action with a visible progress/ETA, not something that implies
  instant/live audio like a real DAW's knobs). Decide the interaction
  explicitly rather than accidentally overpromising: debounced
  auto-re-render vs explicit button. Recommendation: explicit button,
  given a 5-minute render is not going to be sub-second no matter how much
  Phase 2 speeds it up.
- **Docs pages**: a `/beatstudio/docs/` (or `/help` section on-page)
  explaining each control in plain language — what it does, why you'd
  raise/lower it, what a sensible starting range is — written for someone
  who has never mixed audio before, per the "anybody" framing in the
  original request. Link every control's (?) to its matching section.

## Phase 4 — backend/API rework

- Replace `process.py`'s hardcoded `MAIN_STEPS`/`NAMED_STEPS`/mix-gains/EQ
  constants with a parameters blob the frontend POSTs alongside (or after)
  the vocal upload — track list with gain/pan/mute/solo, and the 5 master
  knobs above. `backend.py` needs a new endpoint (or an extension of
  `/api/upload`) to accept and validate this, and `project.json`'s schema
  already models most of it (`tracks[].gain_db/pan/mute` exist; `solo`
  and the master params don't yet).
- Recording cap: raise the client-side `MAX_SECONDS` in `page.html` from
  30 to whatever Phase 0/1's calibration actually supports (target 300+),
  and raise `process.py`'s render timeouts (`preview` currently 300s,
  `master` currently 900s) to match realistic chunked-render wall-clock
  time at the new length.
- Re-render requests need their own session/status lifecycle distinct
  from the initial upload (a visitor may hit "Apply" several times tuning
  knobs before they're happy) — status.json's `status` field needs a
  value for this that's distinguishable from the first render in the UI
  copy.

## Phase 5 — deploy + rollout

- Ship to srv66, `larzscript update` if the deployed interpreter needs the
  version bump from Phase 0/2b's changes.
- End-to-end smoke test with a real 5+ minute recording through the actual
  page (not just `beatstudio.lz` CLI in isolation) — watch `dmesg` live
  during the run, same as the original 50s-ceiling calibration.
- Update `web/README.md` and the repo's main `README.md` to describe the
  new interactive mixer instead of the old fixed pipeline, and add a
  changelog entry.

## Explicit non-goals (for now)

- Real-time/live in-browser audio preview as knobs move (Web Audio API
  effects) — the actual mastering is real server-side Larzscript DSP by
  design (that's the whole point of this app); duplicating it as
  client-side JS just to fake instant feedback would contradict "nothing
  here is faked," the page's own stated principle. If perceived latency
  turns out to be the top complaint after shipping, revisit as a separate
  decision, not bundled into this build.
- No claim of literally unlimited length — Phase 0's empirical ceiling is
  the real cap communicated on the page, not an unbounded promise.

## What actually shipped (real numbers, not estimates)

- **Phase 0 findings**: the deployed interpreter (native-v1.26.0 at the
  time) had `read_file_bytes`/`append_file`/`write_file` but no ranged
  read or in-place byte patch. Interpreted per-sample master-chain
  throughput measured **~10,700 frames/sec on srv66** - a 5-minute stereo
  master would've taken ~10 minutes of pure compute, confirming Phase 2
  was required, not optional. A local sandbox dsp/wav package draft
  already called `_native_*`/`file_size`/`read_file_bytes_range`/
  `patch_file_bytes` that didn't exist anywhere (same bug class in two
  places) - and had never been published to `larzscript-packages`
  either. List memory cost measured directly (VmHWM while building
  1M/5M/13.2M-element lists): **~105-115 bytes/element** - confirms a
  whole 5-minute stereo buffer (~13.2M elements) was never going to fit.
- **Phase 1+2**: implemented the missing native builtins for real in
  `native/larzscript.c` (`_native_master_block`, `_native_mix_add`, plus
  the dsp/wav packages' own documented `_native_biquad_process_buffer`
  etc. and `file_size`/`read_file_bytes_range`/`patch_file_bytes`/
  `_native_pcm16_encode`/`_native_pcm16_decode`), published as
  **native-v1.35.0**, and published the previously-uncommitted dsp/wav
  package versions to `larzscript-packages`. `beatstudio.lz` rewritten so
  every track is one real WAV file, produced via `wav.open_write`/
  `write_chunk`/`close_write` and consumed via true random-access reads -
  never held whole in memory. `_native_master_block` measured
  **~570,000+ frames/sec on srv66** (~50x the interpreted path), verified
  bit-identical to the old per-sample loop and identical across arbitrary
  chunk boundaries.
- **Real srv66 numbers** (1.9GB RAM, real `dmesg` monitoring, nothing
  guessed): a 5-minute beat+vocal master that used to be unsafe past ~50s
  now renders in **~32s at ~518MB peak RSS**; an **8-minute** render lands
  at the **same ~518MB peak** - memory no longer scales with duration at
  all, confirming the chunked design actually decoupled the two.
- **Phase 3+4**: shipped both control-depth options together (per the
  user's choice of "1 and 2") - real multi-track mixer (beat + your
  recording: volume/pan/mute/solo) and the full master chain (Warmth/
  Clarity/Air EQ, Punch threshold+ratio compressor, Loudness limiter
  ceiling) as real parameters, friendly-labeled with real units and a
  linked docs page (`web/docs.html`) per control. New `remaster` command
  (companion to `master`) makes iterating on settings free after the
  first paid render, instead of re-charging on every slider tweak.
- **Phase 5**: deployed live to srv66 2026-08-04 - new interpreter binary
  (backed up the old one first), updated dsp/wav packages, new
  `beatstudio.lz`/`backend.py`/`process.py`/`page.html`/`docs.html`.
  Verified with a REAL end-to-end run through the live production
  API/cron (not a local mock): a 6-minute upload → full paid pipeline in
  67s, then a remix+remaster through the live `/api/params` endpoint in
  57s, correctly free (`spent_cents` unchanged, second master's
  `price_cents` = 0). Clean `dmesg` throughout.
- **Recording cap**: raised 30s → **8 minutes** (480s) on the page,
  backed by the verified-safe numbers above, not a round-number guess.

## Known gaps / honestly not done

- Could not do live browser/visual testing of the new mixer UI - this
  sandbox has no browser or Node available. Validated instead via: full
  manual review of the JS, a structural brace/string-balance check, and
  the real HTTP contract end-to-end via `curl` against the actual
  deployed `backend.py` (valid params accepted+clamped, invalid
  session/still-processing/malformed-body all return the right codes). A
  manual click-through in a real browser is still worth doing.
- `LARZSCRIPT_VERSION` in `native/larzscript.c` was found stuck at
  "1.26.0" despite native tags already at v1.34.0 (a pre-existing,
  unrelated release-process gap) - fixed opportunistically to 1.35.0
  since this session was already bumping it for a real release, but the
  root cause (the version string not auto-bumping with the release
  workflow) wasn't investigated further.
- Drum-pattern editing (the sequencer itself) is still fixed/hardcoded on
  the page - this pass scoped to track mix + master chain, not a step
  sequencer UI. A real, separate feature if wanted later.
