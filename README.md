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
real chain - 3-band EQ, a linked-stereo compressor, a brickwall limiter -
none of it a wrapper around ffmpeg or any other audio tool; every filter
is from-scratch Larzscript, verified against analytically-known values
(see `dsp`'s own README), now with a native-C fast path
(`_native_master_block`) for the hot per-sample loop - ~570,000+
frames/sec measured on the production box, ~50x the pure-interpreted
path, verified bit-identical to it first. Two things genuinely can't be
pure Larzscript, both disclosed as honestly as `crypto`'s documented
X25519 limitation or `net`/`fetch` being kernel-only elsewhere in this
stack: capturing real audio from a microphone (`record`, shells out to
`arecord`/ALSA - no interpreted language has OS audio-driver bindings),
and decoding an arbitrary input format/sample-rate into this app's
standard WAV (`record`/`track-import`, shells out to `ffmpeg` for that
one conversion step only - never for the actual mixing/EQ/compression/
limiting).

Every track - the synthesized beat, a mic recording, an imported WAV - is
stored as one real WAV file and streamed through in fixed-size chunks
(never held whole in memory), so render time and memory stay flat
regardless of length: a verified 8-minute render uses the same ~518MB
peak RSS as a 5-minute one on a 1.9GB production box. See "Chunked
streaming" below for the real numbers.

And mastering is money-native: `master` is a real `pay` from a real
`wallet` that fails closed without funds - the same per-master pricing
real mastering-as-a-service tools (LANDR and similar) actually use, just
enforced by the language's own `unless`/`require` guardrail instead of a
billing system bolted on afterward. `preview` (mix + a safety limiter,
no EQ/compression) is free, and once a project's first `master` is paid
for, `remaster` re-renders with new settings for free - so dialing in a
sound doesn't re-charge the wallet on every tweak.

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
preview (free, mix + safety limiter only, no EQ/compression): .../preview.wav
peak: -1 dBFS
$ larzscript beatstudio.lz master --price=2
mastered -> .../master_1.wav
peak: -1 dBFS, rms: -22.14 dBFS
charged: $2.00, remaining: $18.00
$ larzscript beatstudio.lz remaster --low=-2 --high=4
mastered -> .../master_2.wav
peak: -3.1 dBFS, rms: -24.02 dBFS
remaster is free - already unlocked by this project's first master
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
| `record NAME --seconds=N` | Capture a track from the mic via `arecord` (hosted-only, needs a real microphone - see below). Any length. |
| `track-import PATH --name=NAME` | Add an existing WAV (or anything ffmpeg can decode) as a track - any length, any sample rate/channel count. |
| `mix NAME --gain=DB --pan=P --mute` | Set a track's gain (dB), pan (-1..1), or mute it. |
| `preview` | Free. Mix + a safety limiter only - no EQ or compression. |
| `master --price=DOLLARS [--low=DB --mid=DB --high=DB --thresh=DB --ratio=N --ceiling=DB]` | Paid. The full EQ → linked-stereo compressor → limiter chain; rejects if the project wallet can't afford it. |
| `remaster [--low=DB --mid=DB --high=DB --thresh=DB --ratio=N --ceiling=DB]` | Free, but only after this project's first `master`. Re-renders with new settings without re-charging - built for iterating on a mix. |
| `report` | Show the pattern, tracks (with lengths), last master's stats, and remaining budget. |

## The mastering chain

`dsp.biquad_shelf_new`/`biquad_peaking_new` build a 3-band EQ (low-shelf
@200Hz, peaking @2kHz, high-shelf @6kHz - `master --low= --mid= --high=`
override the defaults). It runs on each stereo channel independently
(a fixed linear filter needs no linking between channels). The compressor
(`--thresh=`/`--ratio=`) is **linked**: one gain per sample-frame,
computed from whichever channel is louder and applied to both, so the
stereo image doesn't wobble the way independently-compressing L and R
would. `--ceiling=` sets the limiter's brickwall ceiling, which *is* the
loudness target for this chain rather than a separate post-hoc
normalize pass - a true peak-normalize needs the final peak before it
can scale anything, a real two-pass problem streaming deliberately
avoids, and compressed+limited material's true peak already sits right
at the ceiling in practice. All three stages run in one native-C pass
per chunk via `_native_master_block()` - see "Performance, honestly"
below.

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

## Chunked streaming, and why it matters

Every track is one plain WAV file, produced via `wav`'s real streaming
writer (`open_write`/`write_chunk`/`close_write` - a placeholder header
written immediately, patched with the real size once it's known) and
consumed via true random-access reads (`read_file_bytes_range`) for
exactly the chunk being mixed right now. Mixing and mastering process a
few seconds of audio at a time and stream straight to the output file -
peak memory is a small constant, not proportional to length.

Measured directly on the production box (srv66, 1.9GB RAM, via real
`dmesg` OOM monitoring, not guessed): an earlier whole-buffer design
OOM-killed at just 60 seconds. This design renders a 5-minute beat+vocal
master in **~32s at ~518MB peak RSS**, and an **8-minute** render lands
at the **same ~518MB peak** - memory genuinely stopped scaling with
duration. The live page's recording cap (8 minutes) is set from these
numbers, not a round guess.

## Performance, honestly

This is an offline "bounce" step, same as any real DAW's - not real-time,
by construction (no interpreted language does real-time audio). The
mastering chain's hot per-sample loop (EQ → linked compressor → limiter)
runs through `_native_master_block()`, a small native-C builtin added to
the Larzscript interpreter itself for exactly this - measured at
**~570,000+ frames/sec on the production box**, versus ~10,700 frames/sec
for the equivalent pure-interpreted per-sample calls (a ~50x speedup),
verified bit-identical to the interpreted path before this file started
relying on it. Drum-voice synthesis itself is still pure interpreted
Larzscript (`dsp.sin_()`/`envelope_exp()` per sample) - `beat-render`
works around its cost by rendering each *unique* pattern once and reusing
that buffer for every repeat in the arrangement (correct, not an
approximation - the same way a real drum machine reuses one sample every
loop), which is what makes a 150-bar/5-minute arrangement render in
seconds instead of minutes.

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
