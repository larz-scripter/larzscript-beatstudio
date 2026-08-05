# PLAN6.md - preview before mix & master, retake in parts

Triggered by two things landing back-to-back: a real production bug
report ("voice editing failed - try again", traced to a genuinely quiet
- not silent - 63s recording that autotrim's silence-scan hard-failed
on) and, mid-fix, an explicit new request from the same user: "i want to
be able to listen to my recordings before mix and master and even edit
it and other thing so that i can retake in parts until i like it then i
can mix and master to finish the process."

## Phase 0 - the production bugs (fixed first, before this feature)

Three real, verified fixes, all from the same incident:

1. **Autotrim hard-fail on a quiet-not-silent recording** -
   `find_leading_silence_end()` returns the FULL track length when
   nothing crosses the -45dB threshold anywhere, which zeroed
   `edited_total` and hard-failed the whole upload. Fixed by clamping
   back to "nothing to trim" (skip, don't fail) when that happens -
   autotrim is a convenience, not a hard requirement.
2. **No real gain-staging on vocal import** - a fixed default gain
   (+2dB, old +12dB manual ceiling) can't rescue a mic recording this
   quiet once mixed against a normally-mixed beat. Added
   `track-import --auto-gain`: measures the real peak, computes a
   starting gain toward a -12dBFS target, clamps to a safety range.
3. **The safety clamp itself was too conservative** - the real
   incident's recording (peak -70.3dBFS) needed +58.3dB to reach
   target; the initial +40dB cap fell well short. Verified via a real
   A/B (vocal muted vs unmuted, diff-signal RMS measured against the
   full mix): at +40dB the vocal sat ~38dB under the beat (diff RMS
   -56.2dBFS vs a -18.2dBFS mix - functionally inaudible); at the real
   +58.3dB target it sits ~20dB under the beat instead (diff RMS
   -37.9dBFS) - present, audible, no clipping. Clamp raised to 60dB.
   The residual gap past that point is an intrinsic limit of a
   recording whose RMS is 23dB below its own peak (mostly near-silence
   with rare louder moments) - not something more gain alone can fix
   without amplifying noise, and the honest stopping point for a bug
   fix rather than open-ended DSP tuning.

## Phase Q - preview before mix & master, retake in parts

The actual feature request. Previously `process_uploaded` bundled the
free preview mix AND the paid master into one uninterruptible pipeline
- a visitor never heard their vocal in context before the first (only)
charge landed. Split into a real checkpoint:

- **"uploaded" status now stops at `preview_ready`** - assemble/import/
  edit/mix/preview, no master, nothing charged. A visitor can listen to
  exactly what beatstudio.lz's own free-preview/paid-master split
  always allowed for, just newly exposed as an actual UI stop instead
  of an invisible intermediate step.
- **Retake reuses the same session and the same endpoint**
  (`POST /api/assemble` again) - process.py detects a retake by
  whether `project.json` already exists for this session: if so, it
  skips init/generate/beat-render/melody-render entirely (the beat's
  already rendered) and goes straight to re-importing the vocal
  onward. A visitor can re-record/re-trim/reorder/delete takes and
  change any vocal FX toggle (autotrim/gate/de-ess/double/harmonize/
  autotune) - the existing take-management UI (already fully built
  for the pre-upload arrangement step) is reused as-is for this,
  nothing new needed there.
- **Stale layered tracks get cleaned up on retake** - `double`/
  `harmonize` each upsert a FIXED track name ("vocal_double"/
  "vocal_harmony"). A retake that turns one of those OFF after a prior
  take had it ON would otherwise leave the old layer sitting in
  project.json, still un-muted, still mixed in (mix_chunk includes
  every non-muted track, not just the ones a mix call explicitly
  touches) - playing STALE audio from the previous take. Explicitly
  muted whenever the flag is off but the track still exists from
  earlier. Verified with a real three-pass local test: pass 1 (no
  extras) -> pass 2 (double+harmony ON, both tracks created, unmuted)
  -> pass 3 (both OFF again) -> confirmed both tracks present but
  `mute: true` after pass 3, and beat.wav's mtime unchanged across all
  three passes (confirming the beat genuinely never re-rendered).
- **New `POST /api/finalize`** - the only action that ever charges the
  wallet, and only valid from `preview_ready`. Triggers
  `finalize_requested`, which re-applies the mix (from params persisted
  at the preview step, so the exact levels a visitor approved carry
  through - including the auto-gain value, not silently reconstructed
  from defaults), masters (paid first time, free after, unchanged
  beatstudio.lz behavior), and publishes the same before/after/stems
  result the old flow always produced.
- **UX**: the existing result deck is reused for the pre-master
  preview (single clip, A/B toggle hidden since there's no "after"
  yet) with two new buttons - "Retake or add more" (scrolls back to
  the recording panel, take state untouched) and "Sounds good - Mix &
  master ($2)" (calls `/api/finalize`). A real, unrelated gap closed
  along the way: an async pipeline failure previously left "Use this
  arrangement" permanently disabled with no retry short of a page
  reload (the click handler's own try/catch only covered synchronous
  fetch failures, not a later `status: "error"` from polling) - now
  re-enabled from the poller's error branch too.

Verified end-to-end locally against the real production recording from
Phase 0's incident: first pass -> `preview_ready`; retake turning
double+harmony ON -> `preview_ready` with both new tracks; retake
turning them back OFF -> `preview_ready` with both correctly muted;
finalize -> `done` with exactly 1 master. 10/10 beatstudio.lz tests
still pass (BEATSTUDIO=larzscript_dev, matching native-v1.40.0 - the
local `larzscript` on PATH turned out to be a stale v1.35.0 build,
caught and ruled out as the real cause of 4 unrelated PSOLA-test
failures before trusting any test result here).
