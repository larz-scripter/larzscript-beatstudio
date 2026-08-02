# larzscript-beatstudio

A beat-making/recording/mixing/mastering studio, written entirely in
[Larzscript](https://github.com/larz-scripter/larzscript) - the
money-native programming language. `beatstudio.lz` is one file, built on
[`dsp`](https://github.com/larz-scripter/larzscript-packages/tree/master/packages/dsp)
(real biquad EQ, a look-up-table compressor, a limiter, peak/RMS/normalize)
and
[`wav`](https://github.com/larz-scripter/larzscript-packages/tree/master/packages/wav)
(real WAV file I/O) for every stage of signal processing.

This isn't a toy example, on any side. The 4 drum voices are real synthesis
(a pitch-swept sine kick, a tone+noise snare, decaying-noise hihat and
clap) driven by a real 16-step sequencer. Mixing sums real tracks with real
per-track gain and equal-power pan. And the professional master runs a
real chain - 3-band EQ, a linked-stereo compressor, a brickwall limiter,
peak normalization - none of it a wrapper around ffmpeg or any other audio
tool; every filter is from-scratch Larzscript, verified against
analytically-known values (see `dsp`'s own README). The one thing that
genuinely can't be pure Larzscript - capturing real audio from a
microphone, which needs an OS audio driver no interpreted language has
bindings for - shells out to `arecord` (ALSA), the same honesty
`crypto`'s documented X25519 limitation or `net`/`fetch` being
kernel-only already establish elsewhere in this stack.

And mastering is money-native: `master` is a real `pay` from a real
`wallet` that fails closed without funds - the same per-master pricing
real mastering-as-a-service tools (LANDR and similar) actually use, just
enforced by the language's own `unless`/`require` guardrail instead of a
billing system bolted on afterward. `preview` (mix + normalize only, no
EQ/compression/limiting) is free, so the whole pipeline is demoable
without a funded wallet.

```
$ larzscript beatstudio.lz init --budget=20 --bpm=140
project created: bpm=140, budget=$20.00
$ larzscript beatstudio.lz step kick 0 on
$ larzscript beatstudio.lz step snare 4 on
$ larzscript beatstudio.lz step hihat 2 on
$ larzscript beatstudio.lz step hihat 6 on
$ larzscript beatstudio.lz beat-render
beat rendered: 1.71s -> /home/you/.larzbeatstudio/beat.wav
$ larzscript beatstudio.lz mix beat --gain=-2
$ larzscript beatstudio.lz preview
preview (free, mix + normalize only, no EQ/compression/limiting): .../preview.wav
peak: -3 dBFS
$ larzscript beatstudio.lz master --price=2
mastered -> .../master_1.wav
peak: -1 dBFS, rms: -22.14 dBFS
charged: $2.00, remaining: $18.00
```

## Install

You need the [Larzscript](https://github.com/larz-scripter/larzscript)
interpreter and the `dsp`/`wav`/`random`/`cli`/`args`/`json`/`fs`/`table` packages:

```
curl -fsSL https://raw.githubusercontent.com/larz-scripter/larzscript/main/install.sh | sh
larzscript pkg install dsp
larzscript pkg install wav
larzscript pkg install random
larzscript pkg install cli
larzscript pkg install args
larzscript pkg install json
larzscript pkg install fs
larzscript pkg install table
```

Then clone this repo (or just download `beatstudio.lz`) and run it:

```
git clone https://github.com/larz-scripter/larzscript-beatstudio
cd larzscript-beatstudio
larzscript beatstudio.lz init --budget=20 --bpm=140
```

Data (project state + rendered WAVs) lives in `~/.larzbeatstudio/` by
default (override with `--file=PATH` on any command).

## Commands

| Command | What it does |
|---|---|
| `init --budget=DOLLARS --bpm=N` | Start a project. |
| `step VOICE STEP on\|off` | Program the 16-step pattern (`VOICE` is `kick`/`snare`/`hihat`/`clap`, `STEP` is 0-15). |
| `beat-render` | Free. Synthesize the 4 drum voices per the pattern and add the loop as the `beat` track. |
| `record NAME --seconds=N` | Capture a track from the mic via `arecord` (hosted-only, needs a real microphone - see below). |
| `track-import PATH --name=NAME` | Add an existing WAV as a track (no mic needed). |
| `mix NAME --gain=DB --pan=P --mute` | Set a track's gain (dB), pan (-1..1), or mute it. |
| `preview` | Free. Mix + normalize only - no EQ, compression, or limiting. |
| `master --price=DOLLARS [--low=DB --mid=DB --high=DB]` | Paid. The full EQ → linked-stereo compressor → limiter → normalize chain; rejects if the project wallet can't afford it. |
| `report` | Show the pattern, tracks, last master's stats, and remaining budget. |

## The mastering chain

`dsp.biquad_shelf_new`/`biquad_peaking_new` build a 3-band EQ (low-shelf
@200Hz, peaking @2kHz, high-shelf @6kHz - `master --low= --mid= --high=`
override the defaults). It runs on each stereo channel independently
(a fixed linear filter needs no linking between channels). The compressor
is **linked**: one gain per sample-frame, computed from whichever channel
is louder and applied to both, via `dsp.compressor_gain()` - so the stereo
image doesn't wobble the way independently-compressing L and R would.
`dsp.limit()` and `dsp.normalize_to()` already operate correctly on the
whole interleaved buffer as-is (they're global/pointwise, not per-sample
stateful, so there's nothing to link).

## A real Larzscript interpreter bug, found and worked around

Building this surfaced a genuine native-interpreter bug (filed as
[larzscript#4](https://github.com/larz-scripter/larzscript/issues/4)):
after a script has done enough prior allocation (rendering a few seconds
of 22050 Hz audio easily clears the threshold), a dict literal whose later
values are calls into an **imported package's** function gets its earlier
string keys corrupted. `beatstudio.lz` works around it by always computing
such calls into a `let` first and using the local, never an inline
cross-module call, inside a dict literal - see the comment in `cmd_master`
in `beatstudio.lz`.

## Performance, honestly

This is an offline "bounce" step, same as any real DAW's - not real-time,
by construction (no interpreted language does real-time audio). How long
that bounce takes depends a lot on the machine: on the constrained
sandbox this was developed on (~370K simple ops/sec benchmarked), a full
stereo master through the whole EQ→compressor→limiter→normalize chain
took on the order of a minute for a couple of seconds of audio; the same
test suite ran in **~18 seconds total** (both scenarios, including a
2-track master) on a normal GitHub Actions runner - 4-5x faster. Expect
real hardware to render considerably faster than the sandbox numbers,
still meaningfully slower than a compiled DAW's bounce. That's the real
cost of doing every DSP stage from scratch in an interpreted language
with no dependencies - the same trade-off
[`raytrace`](https://github.com/larz-scripter/larzscript-packages/tree/master/packages/raytrace)
makes with deliberately tiny render resolutions.

## Recording, honestly

`record` shells out to `arecord` because no interpreted language - this
one included - has direct OS audio-driver bindings; it's the one stage
that can't be pure Larzscript, the same category as `crypto`'s documented
X25519 limitation. It's implemented and code-reviewed for correctness, but
the sandboxed environment this was built in has no microphone permission
to verify a live capture end-to-end - `track-import` exists specifically
so the rest of the pipeline (mixing multiple tracks, mastering) is fully
testable without one.

## Testing

Nothing here is actually random despite the noise-based synthesis - the
`random` package's PRNG starts from the same fixed seed every fresh
process (nothing in this app calls `seed()`), so a given pattern renders
byte-identically every run, verified independently before writing the
golden files below.

```
sh tests/run_tests.sh
```

## The persistence pattern

`wallet`/`pay` are real Larzscript constructs, but they're scoped to *one
script execution* - there's no built-in way for a wallet's balance to
survive between separate `larzscript beatstudio.lz ...` invocations. Each
run rehydrates the project's remaining budget as a real `wallet` from
cents stored in `project.json`, performs whatever real `pay`/`unless` this
invocation asked for, then writes the resulting balance back out. Same
pattern every `larzscript-*` showcase app in this ecosystem uses - see
[`larzscript-budget`](https://github.com/larz-scripter/larzscript-budget)'s
`budget.lz` for the fuller writeup.

## License

MIT
