# PLAN8 — auto-level, client-side volume editor with instant preview, final-mix download

User report (2026-08-05, same day as PLAN7): "the recording when i finish
is not consistent on volume, some parts low some parts high can we make
it consistent after recording... a voice editing panel that shows and i
can increase and decrease parts easily and hear the outcome immediately
before clicking apply and render... give way to download the completed
mixed and mastered product."

## Phase R1 — Auto-level (real fix, on by default)

The existing `track-import --auto-gain` (from PLAN6) sets ONE overall
gain for a whole take based on its measured peak/RMS - it never
addressed WITHIN-take inconsistency (a take that's quiet in one section
and loud in another). New `voice-edit --autolevel`: a real compressor
pass (`dsp.compressor_new(-24.0, 4.0, 8.0, 120.0, SAMPLE_RATE)` -
same self-compression call, `dsp.compressor_process_buffer`, the
per-track mixer strip already uses, just tuned for evening-out rather
than character shaping) applied in the existing chunked voice-edit loop,
after gate/de-ess. Verified with a synthetic 8s take alternating quiet
(0.03 amplitude) and loud (0.4 amplitude) 2s sections: the loud/quiet
RMS gap dropped from ~22.5dB (untreated) to ~11.3dB after autolevel -
consistent with the 4:1 ratio/-24dB threshold math (quiet segments sit
below threshold and pass through unchanged, loud ones get pulled back
toward it). New `fx-autolevel` checkbox in "Clean up your vocal,"
**checked by default** so this fixes the reported problem out of the
box, not as a feature a visitor has to discover.

## Phase R2 — client-side volume editor with instant Web Audio preview

New "Even out your vocal's volume" panel (appears once a take exists):
8 equal-section sliders (-9 to +9dB each). "Preview with these levels"
schedules every take via Web Audio (same technique "Preview full
arrangement" already used) routed through one master GainNode with
automation - `setValueAtTime` at the arrangement start, then
`linearRampToValueAtTime` at each section's CENTER point - plus the
generated beat playing alongside via the existing gapless-loop mechanism
(PLAN7) for real context. Nothing touches the server until "Use this
arrangement" sends the same 8 dB values as `fx.gainEnvelope`.

New `voice-edit --gain-envelope=DB,DB,...` applies the identical curve
server-side (`envelope_db_at()` interpolates between segment centers,
matching the client's ramp shape exactly, so what's previewed is what
renders). **Real bug caught by measurement, not review**: a first
version applied one native `_native_fade_buffer` ramp per fixed 10s
CHUNK_SAMPLES processing chunk, using the envelope's value at that
chunk's own start/end - correct only when a chunk never spans a
segment-center breakpoint. A test with 4 sections over a 24s clip (6s
sections, smaller than the 10s chunk) proved this wrong: a center meant
to read +6dB measured -4dB, because the chunk spanning it used a nearly
flat ramp between two unrelated endpoints, silently erasing the actual
peak. Fixed by switching the whole voice-edit loop from a fixed
`c*CHUNK_SAMPLES` grid to a position-tracking loop that shrinks any
individual chunk to stop exactly at the next segment-center breakpoint
when a gain envelope is active - the ramp applied to any one chunk is
then always a genuinely straight piece of the true curve, never an
approximation. Re-verified: all 4 section centers measured within
0.1dB of their target (+6.00, -5.91, +6.00, -5.91 against a
+6/-6/+6/-6 envelope). Zero new native code - reused `_native_fade_buffer`
(already shipped for fade-in/out) end to end.

## Phase R3 — download the finished master

`after_url` (the "done" status's real final master, already playable
via "Your mastered track") had no direct download affordance - only
individual stems did. Added a plain `<a download>` "Download your mixed
& mastered track" button. Zero backend change - same file, just exposed.

## Verification

10/10 beatstudio.lz tests pass (loop-structure refactor didn't touch
existing fade/gate/deess/autotrim behavior when no gain-envelope is
requested - this_len sizing is unchanged unless `do_gain_env` is true).
Real synthetic-signal tests: autolevel's RMS-gap reduction, and the
gain-envelope's per-section accuracy (both before AND after finding/
fixing the chunk-boundary bug). End-to-end `process_uploaded` smoke test
with `autolevel: true` + `gainEnvelope: [3, -3]` reaches `preview_ready`
cleanly. `backend.py`'s `normalize_manifest` reviewed by hand for the new
`autolevel`/`gainEnvelope` fields (clamped, length-capped) - couldn't be
executed directly in this sandbox (its `ThreadingHTTPServer` import
needs Python 3.8+, sandbox only has 3.6; srv66 runs 3.12) - planned a
real live HTTP `/api/assemble` smoke test against production instead,
same discipline prior rounds used for anything sandbox-untestable.

New docs.html entries: Auto-level, the volume-editor section, and
downloading the finished master.

## Deployment

`beatstudio.lz`/`web/backend.py`/`web/process.py`/`web/page.html`/
`web/docs.html` deployed to `/opt/beatstudio/` + `/var/www/larzos/`
(originals backed up first), service restarted. No native release
needed - zero new native C code this round either.
