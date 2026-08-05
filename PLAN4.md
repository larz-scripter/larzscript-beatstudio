# PLAN 4: real full-song length, autotune, true pitch-shift, real reverb

Status: **SHIPPED and DEPLOYED, 2026-08-05** (larzos.com/beatstudio/). All
four phases (I/J/K/L) built, tested, and live. Native interpreter
released as **native-v1.40.0**. This was the hardest round of the whole
project - PSOLA/pitch detection alone went through 4 real, measured bugs
before landing correct (see Phase J below and the native builtins' own
comments).

**What shipped:**
- **I**: real verse/chorus/bridge song structure, length-scalable up to
  5 minutes (`--target-seconds=`), instead of one groove repeated with a
  single fill. A 300s target produced a genuine 155-bar/290.6s
  arrangement locally; a live 180s request produced a real 170.6s song
  in production.
- **J**: real pitch correction (autotune) - autocorrelation pitch
  detection + TD-PSOLA resynthesis, no FFT anywhere. A strength slider
  (0=off/true no-op, low=natural, 1.0=hard-snap) covers both use cases
  per how this was scoped. 2 new native builtins
  (`_native_detect_pitch_track`/`_native_psola_shift`).
- **K**: `double`/harmony upgraded to the SAME PSOLA engine by default -
  genuinely tempo-preserving now (`--resample` kept as an explicit
  fallback). A real algorithmic reverb (4 parallel Schroeder/Freeverb-
  style combs) built entirely on the existing delay-line primitive, zero
  new native code.
- **L**: all of the above wired into the live pipeline - a song-length
  picker, autotune strength/key/scale controls, reverb amount/decay,
  updated docs.html.

**4 real, measured bugs in the native PSOLA/pitch-detection code**, each
found by re-detecting pitch on the actual OUTPUT and comparing against
an independently-computed expected value - never by trusting the
construction:
1. Grains placed at the coarse analysis-hop spacing left silent gaps
   wherever period < hop_size (true for any real vocal pitch) - output
   pitch hadn't moved at all.
2. Fixed the spacing, but changed pitch by resampling grain CONTENT
   while keeping the same landing position it was read from - still
   didn't work: overlap-add where read position always equals write
   position is provably just the original signal again
   (`acc[idx]/wsum[idx]` reduces to exactly `buf[idx]` algebraically).
   The real fix needed two independent walks (analysis reads raw grains
   at their own rate; synthesis writes them at `period/ratio` instead of
   `period` - the retrigger-RATE difference IS the pitch shift).
3. The pitch DETECTOR itself hit a classic autocorrelation octave-down/
   subharmonic bug (a 2nd/3rd harmonic period often correlates almost as
   strongly as the true fundamental) - fixed by preferring the shortest
   genuine LOCAL PEAK within 8% of the global max.
4. A naive first version of fix #3 ("any point above threshold," not
   requiring a true local max) broke pure-tone detection instead - a
   bare sine's autocorrelation is one smooth lobe with no real secondary
   peak, and the naive threshold grabbed a spurious point on that lobe's
   shoulder (220Hz measured as 234.57Hz). Also revealed a real test-
   methodology lesson: a pure sine can't validate PSOLA at all (summing
   time-shifted copies of one sine algebraically stays at that same
   frequency) - a harmonically-rich sawtooth is the valid test signal.

**Final verified accuracy** (sawtooth test signal, harmonically rich
like a real voice): pitch-up shift landed within 0.3% of target,
pitch-down within 0.45%, autotune correction within 0.5% - all at the
theoretical limit of integer-sample-period quantization at 22050Hz, not
approximate.

**Live production verification** (not just local): a real 180s
full-song request produced a genuine 170.6s house track; a real vocal
upload with autotune strength=0.85 + double, pulled directly off the
production server (not the mixed-down master, which is too noisy for a
clean pitch check), measured 220.5Hz against an expected ~219.4Hz; a
reverb+preset rerender changed 97.3% of output samples vs. baseline.
35/35 native tests, 8/8 beatstudio.lz tests. srv66 disk unchanged
(26G/49G), zero errors in beatstudio_process.log across the whole
session.

## Why

After PLAN3 shipped, user asked for a second round: recording and beat
generation should genuinely reach a **full song length** (up to 5
minutes), plus "more tooling effects like autotune and others and more"
to bring the output to the highest standard possible. Asked two scoping
questions via AskUserQuestion:
- **Autotune style**: a strength slider, natural by default (0 = off,
  low = subtle correction, high = the obvious hard-snap effect) - not a
  binary natural-vs-hard-tune choice.
- **Song length**: a length picker, short (~15-45s) by default for fast
  genre/vibe iteration, up to 5 minutes when actually recording a full
  take - not full-length by default.

**Recording itself already reaches 5 minutes** - `MAX_SECONDS = 480` (8
minutes) has been live since PLAN2. The real gap was entirely on the
generation side: `generate --bars=N` capped at 16 bars (`BARS_RANGE` in
backend.py), and even at the max, the fixed
`"intro*2,main*N,fill,main*2,intro"` template only reaches ~45-60s at a
typical bpm - nowhere near a full song, and structurally just one groove
repeated with a single fill, not real verse/chorus variation.

## Phase I - full-song structured arrangement

**New pattern types**: `gen_chorus(genre)` (a distinct, denser/more
energetic section - NOT the same construction as `fill`, which is a
one-bar turnaround; chorus needs to sound like a real repeatable section
on its own, so it gets fixed per-genre embellishments layered onto
`gen_main`'s groove, not just `fill`'s probabilistic extra hits) and
`gen_bridge(genre)` (a stripped-down breakdown - fewer active voices,
real contrast partway through a song, the way real songs pull back
before the final choruses).

**Length-aware arrangement builder**: `build_full_arrangement(patterns,
target_bars)` replaces the fixed template with a real verse/chorus
structure (`intro -> [verse*4, chorus*4] x N -> bridge*2 -> fill ->
chorus*4 -> outro`), where N (how many verse/chorus cycles) scales to
approximately fill `target_bars` - a real song reaches 5 minutes by
having more verse/chorus repeats, the same way real songs do, not by
stretching one section arbitrarily long.

**CLI**: new `--target-seconds=N` flag on `generate`, computing
`target_bars` from the project's bpm before building the arrangement.
Omitting it keeps the existing short `--bars=N` behavior exactly as
before - no change for anyone not asking for a longer song.

## Phase J - real pitch correction (autotune)

The single biggest technical piece of this round. `dsp` has zero
FFT/spectral code (confirmed again, unchanged since PLAN3's own
disclosure) - a phase vocoder is out. **TD-PSOLA (time-domain
pitch-synchronous overlap-add)** is the real, actually-used-in-production
technique that needs no FFT at all, just time-domain period detection +
windowed overlap-add - genuinely buildable here.

1. **Pitch detection**: per-frame normalized autocorrelation restricted
   to a plausible vocal range (~70-500Hz -> lag range ~44-315 samples at
   22050Hz), the standard time-domain F0 estimator. A voiced/unvoiced
   gate (correlation strength threshold) skips correction on
   silence/noise/consonants instead of chasing a nonsense pitch there.
2. **Target pitch**: snap the detected frequency to the nearest note -
   default full chromatic (any semitone), with an optional
   `--key=`/`--scale=major|minor` to restrict correction to only the
   notes in that scale (matches real pitch-correction tools' "scale
   lock"), reusing Phase E's `CHROMATIC_RATIOS`/`note_freq`.
3. **Strength**: `applied_ratio = 1 + (target_ratio - 1) * strength`.
   strength=0 is a true no-op (bit-identical passthrough); low values
   nudge partially toward the target (natural); strength=1 snaps fully
   and instantly per-frame with no smoothing - the classic hard-tune
   robotic effect. One control genuinely covers both, per the user's
   answer.
4. **Resynthesis**: TD-PSOLA - extract pitch-synchronous grains (2
   periods wide, windowed) around detected pitch marks, overlap-add them
   back at a new spacing (`period / applied_ratio`) so pitch changes
   while grain CONTENT length - and therefore overall duration - is
   preserved, unlike the crude resample trick `double` uses.

**Native for speed** (this runs over potentially 5+ minutes of audio,
the same reasoning every other whole-track DSP pass in this file
already needed native acceleration for): `_native_detect_pitch_track`
(autocorrelation per frame across a buffer) and `_native_psola_shift`
(grain extraction + overlap-add given a pitch/ratio track), added to the
interpreter the same disclosed way every prior native builtin was.

**Verification plan (this can't be listened to, same constraint as every
prior round)**: pitch-detect a pure test tone and confirm the measured
frequency matches within a small tolerance; run correction on a
synthetic swept/off-key tone and re-detect the OUTPUT's pitch to confirm
it actually moved toward the target; check for click/discontinuity
artifacts via sample-to-sample delta spikes at grain boundaries, not
just level/duration stats - the same escalation from "didn't crash" to
"real signal analysis" every earlier phase of this project went through,
applied up front this time instead of after a mistake.

**Still explicitly NOT in scope**: true spectral noise reduction (a
genuinely separate problem from pitch correction, still needs FFT-style
spectral analysis PSOLA doesn't provide) - flagged again, not silently
promised.

## Phase K - real tempo-preserving harmony + a real reverb

- **Upgrade `double`**: once Phase J's PSOLA engine exists, it's the
  correct tool for harmony/doubling too - shifts pitch by a real
  interval WITHOUT changing tempo, fixing the honest limitation PLAN3's
  `double` disclosed (resample-based, changed pitch and tempo together).
  Old resample-based behavior kept available as a fallback flag in case
  PSOLA is unavailable for a given input, not removed outright.
- **Real reverb**: a Schroeder/Freeverb-style network (parallel comb
  filters into a series allpass chain) built on Phase F's existing
  delay-line primitive (each comb/allpass IS a delay line with different
  feedback/mix) - the "good v2" PLAN3 flagged once the delay-line
  primitive was proven. A genuine algorithmic reverb, not another
  single-tap echo.

## Phase L - UX wiring

- A length control (short/1min/2min/5min or a slider) next to the genre
  picker, short selected by default per the user's answer.
- Autotune: strength slider (0-100%, 0 default) + optional key/scale
  lock, in the vocal's Advanced section alongside the existing
  EQ/compressor/delay controls.
- `double`/harmony UI copy updated once it's genuinely tempo-preserving -
  the "changes pitch and timing slightly" disclaimer only applies to the
  fallback path now, not the default one.
- A real Reverb amount control (replacing/extending the existing echo
  send with a wet/dry blend into the new algorithmic reverb).
- docs.html entries for every new control, matching the existing
  plain-language style.

## Open questions

- Autotune's default key/scale when none is set on the project - same
  answer as Phase E's melody key default (per-genre A minor/C major
  family), or should autotune default to fully chromatic (any note
  allowed) regardless of the project's melody key? Leaning fully
  chromatic by default (least surprising - "fix my pitch" shouldn't
  silently restrict which notes are considered "in tune" unless asked),
  scale-lock opt-in.
- PSOLA's grain window size/period-detection range are tuned for a
  typical adult vocal range - very low (deep bass vocal) or very high
  (falsetto/child) voices may need different tuning; flagged for
  real-world feedback once live, not blocking this round.
