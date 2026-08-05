# PLAN 5: chord-aware auto-harmony, real loudness targeting, stems, UX polish

Status: **SHIPPED and DEPLOYED, 2026-08-05** (larzos.com/beatstudio/).
All four phases (M/N/O/P) built, tested, and live. No new native
interpreter release needed this round - every new capability (the
chord-aware harmonizer, LUFS metering) was built entirely on native-
v1.40.0's existing PSOLA/pitch-detection/biquad primitives.

**What shipped:**
- **M**: `harmonize NAME --interval=third|fifth` - a genuinely chord-
  aware diatonic harmonizer (the active scale is re-derived per-frame
  from whichever chord is actually sounding, not one fixed scale).
  Verified with two independently-computed tests: a major chord's root
  harmonized to a major third (164.55Hz vs 164.81Hz expected, 0.16%
  error); the same request against a minor chord correctly produced a
  MINOR third instead (262.5Hz vs 261.63Hz, clearly distinct from the
  277.18Hz a wrong major-third result would have given).
- **N**: real (approximated, honestly disclosed) LUFS integrated-
  loudness metering + `--target-lufs=` streaming target. A real,
  disclosed two-pass design - a cheap measurement-only pre-pass, the
  expensive native EQ/compressor/limiter still runs once. Verified:
  perfectly linear level response (exact 6dB-in/6dB-out), correct
  frequency weighting (bass reads quieter, presence range reads
  louder), and monotonic real-world behavior (-14 target measured
  -16.65 actual on live production, -23 target measured -22.28 -
  precision genuinely degrades near the limiter for loud targets, as
  disclosed in the docs, not hidden).
- **O**: real stem export (every rendered track published individually,
  not just the final mix) + staged processing status (real stage names
  like "Correcting pitch..."/"Mastering your track..." instead of one
  static message for the whole wait).
- **P**: all of the above wired into the UI, plus a real-time level
  meter during PLAYBACK (not just recording).

**Live production verification**: a real vocal upload with autotune
strength=0.7 + harmonize produced 4 real published stems (beat/melody/
vocal/vocal_harmony); staged status genuinely showed real progress
("Rendering the beat..." -> "Rendering bass & chords..." ->
"Mixing your preview..." -> "done") across the actual wait, not a
static message; a real -14 LUFS target rerender measured -16.65 LUFS
actual on the live master. Zero errors in beatstudio_process.log, srv66
disk unchanged (26G/49G). 10/10 beatstudio.lz tests, native interpreter
unchanged from round 2's native-v1.40.0 (35/35 tests still pass, no
new native code this round).

## Why

Third round. User asked for more, "especially on voice," plus "others
and more," aiming for "highest standard possible," and asked for the UX
to get "even more seamless." Asked two scoping questions:
- **Auto-harmony**: should `double`/harmony become chord-aware (follows
  the song's actual key/chord, not a fixed interval) - answer: **yes**.
- **Loudness**: should mastering get a real LUFS streaming target -
  answer: **yes**.

## Phase M - chord-aware auto-harmonizer (voice)

New `harmonize NAME --interval=third|fifth [--as=NEWNAME] [--gain=DB]`,
distinct from `double` (which stays a fixed-interval quick effect).
Reuses every existing piece - `_native_detect_pitch_track`,
`_native_psola_shift`, the `SCALES` table, Phase E's `p["chords"]`/
`p["key_root_semitone"]` - no new native code.

**How it actually follows the song**: for each detected (voiced) frame,
compute which BAR of the arrangement that frame's absolute sample
position falls in (`bar_dur` from bpm, same math `render_pattern`
already uses), look up `p["chords"][bar_index]` (clamped/held at the
last chord if the vocal runs longer than the progression), and use
THAT chord's own quality to pick a local scale (major-family chord
qualities -> major scale, minor-family -> natural minor) rooted at
THAT chord's own root - not one fixed project-wide key. This is what
makes it genuinely chord-aware: the harmony scale itself changes bar to
bar as the chords change, matching how a real backing-vocal arranger
follows the chart.

**The interval itself is diatonic (scale-degree), not chromatic**: a
"third" means +2 scale-degree steps within whichever 7-note scale is
in effect at that instant - this is what makes a diatonic harmonizer
automatically choose major vs. minor thirds depending on context
instead of a single fixed semitone count, the standard real technique
real vocal harmonizers use. Needs two new small helpers:
`semitone_to_degree` (quantize a detected frequency into the active
scale, return its degree index) and `degree_to_semitone` (the inverse,
handling octave wrap when the target degree over/underflows the
7-note scale).

Creates a NEW track (same convention as `double`, not in-place like
`autotune`/`voice-edit`) - a harmony line is meant to sit alongside the
lead vocal, not replace it.

## Phase N - real LUFS-style loudness metering + a streaming target

Real integrated loudness (the measurement Spotify/YouTube/Apple Music
actually use) instead of the existing Loudness control just being a
limiter ceiling. **Honestly scoped as an approximation, not certified
ITU-R BS.1770**: true K-weighting + 2-stage relative gating is a lot of
exact-coefficient machinery; this uses the SAME `dsp.biquad_shelf_new`
already in the stack to approximate K-weighting's shape (a high-pass
around the low end + a high-frequency shelf boost, the two components
that actually matter perceptually) and a simple absolute silence gate
instead of ITU's relative 2-stage gate. Disclosed as an approximation in
the UI copy and docs, not presented as certified-compliant.

New `dsp.lufs_new(sample_rate)` / `dsp.lufs_process_buffer(state, l, r)`
(accumulates K-weighted mean-square, mutates state) /
`dsp.lufs_result(state)` (returns the integrated LUFS number so far).

**A real, disclosed two-pass design** for `--target-lufs=`, the one
deliberate exception to this file's usual single-pass streaming
discipline (same rationale `double`/`autotune` already established for
being whole-buffer): a cheap MEASUREMENT-ONLY pass (mix only, no EQ/
comp/limiter, no file written) determines the current loudness, then a
pre-gain computed from the difference feeds into the real single-pass
render exactly like an extra `master_params` field would. The expensive
native EQ/compressor/limiter stage still only runs once - only the
(already-fast, native) mixing step runs twice.

## Phase O - stem export + UX polish

- **Stem download**: the individual already-rendered tracks (beat.wav,
  melody.wav, vocal's final edited file) copied into the results
  directory alongside before/after.wav, with download links added to
  the result panel - a real "professional" deliverable (remixing/
  further production elsewhere needs the separated parts, not just the
  final 2-track mix).
- **Real-time playback level meter**: the same AnalyserNode technique
  the recording meter already uses, now also attached to the result/
  demo decks during playback - visual feedback while listening, not
  just while recording.
- **Staged processing status**: today the page shows one generic
  "processing" message for the whole pipeline (which can be 10s or
  several minutes depending on song length/effects). process.py already
  runs through named stages (beat render, melody render, vocal import,
  voice-edit, autotune, double/harmonize, mix, preview, master) -
  writing the CURRENT stage name into status.json as each one starts
  lets the page show real progress ("Mastering..." vs a static
  "processing...") instead of one unchanging message for the whole
  wait - a genuine "more seamless" UX improvement, not decorative.

## Phase P - UX wiring

- `harmonize` exposed in "Clean up your vocal" alongside `double` -
  an interval choice (third/fifth) + on/off, matching the same
  simple-toggle pattern the other vocal FX already use.
- `--target-lufs=` exposed as a simple toggle + streaming-platform
  presets (e.g. "Spotify -14 LUFS", "YouTube -14 LUFS", "Off/manual")
  next to the existing manual Loudness slider - picking one sets the
  target, manual ceiling still available for full control.
- Stem download links + the staged status text wired into the result
  panel/status poller.

## Open questions

- Harmonize interval default: proposing "third" (the single most common
  backing-harmony interval) as the default choice, "fifth" as the
  secondary option - confirm-or-adjust once heard, not blocking this
  round.
- LUFS approximation accuracy hasn't been validated against a reference
  LUFS meter (no such tool available in this sandbox) - the K-weighting
  shape is a reasoned approximation of the real ITU curve, not verified
  sample-for-sample against a certified implementation. Flagged
  honestly, not silently presented as certified.
