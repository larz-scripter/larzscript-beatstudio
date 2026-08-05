# PLAN7 — uniqueness, upload-your-own-beat, recording-playback fixes, vocal presets

User report (2026-08-05, same day as PLAN6): (1) beat/drum/chord/melody
auto-generation was producing repeated tracks — "autogeneration must
always generate new tracks... maybe we need a database for it"; (2) after
generating, download the beat to learn to play it, and a way to upload
your own beat and record over that instead; (3) more tooling/effects
across all phases, especially voice, and make it seamless. Mid-session,
a 4th real bug was reported: the beat playing back DURING recording
"doesn't always sound right... sounds blurring" compared to right after
generating it.

## Phase Q1 — root-causing "repeated tracks" (real fix, not just a database)

Investigated before reaching for a database: `genre_default_key(genre)`
and `genre_progression(genre)` were 100% FIXED per genre (every "pop"
song ever generated used the identical key and I-V-vi-IV progression),
and `gen_main`'s kick/snare/clap/tom skeleton was also fixed per genre —
only hi-hat density and a couple of single-hit flourishes ever consulted
`rng.random()`. Two regenerations of the same genre were musically
near-identical regardless of seed. This is the actual bug the user was
describing, not primarily a collision-probability problem.

**Real fix**: `genre_progressions(genre)` now returns a POOL of 2 real,
distinct progressions per genre; `genre_keys(genre)` returns 3 real key
choices (±3/-2 semitones around the documented center — still the same
comfortable bass/pad register, genuinely different tonal center);
`gen_main`/`gen_intro`/`gen_fill`/`gen_chorus`/`gen_bridge` all take a
`variant` (0 or 1) with a genuinely different drum skeleton per variant
per genre (house's four-on-the-floor kick stays fixed on purpose — it's
the genre's actual identity — variety lives in the off-beat percussion
instead). `cmd_generate` rolls `variant`/`progression`/`key` once via
`rng.randint`/`rng.choice` right after `rng.seed(seed)` (in that fixed
order, so a given `--seed` stays fully reproducible), all three also
`--flag`-overridable. Verified: two renders at different seeds produce
byte-different `beat.wav` (confirmed via `cmp`); explicit
`--variant=0 --progression=1 --key=5` overrides work as printed.

**Belt-and-suspenders on top**: `process.py`'s new `_project_fingerprint`
hashes the actual chosen musical identity (genre/bpm/variant/progression/
key/melody/arrangement-length — deliberately NOT the seed or raw audio,
so two different seeds landing on the same identity by chance still
count as "the same track"). A sqlite ledger (`fingerprints.db`, stdlib
`sqlite3`, zero new dependency) tracks what's been handed out in the last
6h; a real collision (tested: identical request, same seed, submitted
twice) triggers a deterministic seed-bump retry loop (large prime offset
per attempt, up to 8 attempts, fail-open after that so a small genre pool
never blocks a visitor from ever getting a beat). **Real bug caught by
testing**: the bumped seed only lived in the in-memory `beat` dict — the
retake path (`process_uploaded`) re-reads `beat.json` from disk to
rebuild the SAME beat for the actual recorded take, so without persisting
the bumped seed back to `beat.json`, a bumped preview and the real
recorded beat would have silently diverged. Fixed by writing `beat.json`
back after the retry loop settles. **Also caught**: `INSERT ... ON
CONFLICT DO UPDATE` needs sqlite ≥3.24 — the local dev box only had
3.22 (srv66 has 3.45, so this would never have surfaced in production,
but `INSERT OR REPLACE` is simpler anyway for a 2-column table and works
everywhere).

7/7 generate-based golden tests regenerated (variant/key/progression are
now part of the printed output) and reviewed diff-by-diff to confirm
every difference was the intended new variety, not a regression. 10/10
pass.

## Phase Q2 — upload your own beat + download the generated one

**Download**: `beat_url` from `beat_ready` already points at the real,
complete rendered instrumental (same file the deck plays) — just needed
a plain `<a download>` on the page. No backend change.

**Upload your own beat**: new `POST /api/beat-upload?session=X` (raw
audio body, mirrors `/api/take`'s shape) writes `beat_upload_raw.<ext>`
and a `beat.json` with `customUpload: true`. `process.py`'s
`process_beat_requested`/`process_uploaded` both branch on that flag:
skip `generate_beat`/`beat-render`/`melody_render`/the uniqueness ledger
entirely (none of that applies to a visitor's own file), and call new
`import_uploaded_beat()` — which turned out to need only ONE line of
real work: `track-import <raw file> --name=beat`. **Real bug caught by
testing**: a first attempt pre-decoded the upload via ffmpeg to a file
ALSO named `beat.wav` before calling `track-import` — but
`cmd_track_import` in beatstudio.lz already calls its own
`decode_to_wav` internally, writing to `dir_of(path)+"/beat.wav"` for a
track named "beat" — output-equals-input, ffmpeg refused ("Output same
as Input"). Fixed by dropping the redundant pre-decode and just handing
`track-import` the raw uploaded file directly (it already accepts any
ffmpeg-decodable container). No melody is generated under an uploaded
beat — its key/chords are unknown, and guessing would likely clash.
Verified end-to-end locally: upload → `beat_ready` → record a take →
`preview_ready` → `finalize` → `done`, with correct stems (`beat`,
`vocal`) and no other regression to the existing generated-beat path
(confirmed via a combined smoke test: generated beat + melody + vocal +
double, gate/de-ess/autotune all active, mastered to `done`).

## Phase Q3 — the recording-playback bug ("sounds blurring")

Root-caused two real, independent issues once actually investigated
(not the sample rate, which is a real but long-standing/disclosed
performance tradeoff, not a new-sounding intermittent bug):

1. The beat plays during recording via `<audio>.loop = true`. Native
   HTMLMediaElement looping is NOT sample-accurate in any browser — it
   re-triggers via an internal seek, leaving an audible click/gap at
   every loop boundary. Invisible on a one-off preview listen, but a
   "quick preview" beat is only a few seconds looping for up to an
   8-minute take — dozens of glitches per take. Fixed by decoding the
   beat once into a real `AudioBuffer` and looping it via
   `AudioBufferSourceNode` (genuine sample-index wraparound in the
   render graph, no seek, no click) instead, with the native `<audio>`
   loop kept only as a decode-failure fallback.
2. `getUserMedia({audio: true})` lets the browser pick its own mic
   constraints — on Chrome/Android in particular that means hardware
   echo-cancellation/noise-suppression/auto-gain get enabled, which some
   Android versions implement by dropping the WHOLE OS audio session
   into "voice communication" mode, a mode tuned for phone-call
   intelligibility that audibly processes/muffles OTHER audio playing
   at the same time — including the page's own beat, playing out the
   speaker to perform against. Fixed by explicitly requesting
   `echoCancellation: false, noiseSuppression: false,
   autoGainControl: false`. Some beat bleed into the take is an
   accepted tradeoff (the existing gate/de-ess clean that up); a beat
   you can't hear clearly to perform against is not.

No browser available in this environment to click-through — verified via
esprima syntax-check of the modified `<script>` block (all 6 inline
blocks still parse) and full HTML tag-balance check, same discipline
every prior round used for page.html edits.

## Phase Q4 — more tooling, especially voice, seamless to use

Scoped to a real, bounded, zero-new-DSP-risk addition rather than new
native code this round: **one-click vocal tone presets** (Clean/Warm/
Radio/Bright-Pop), same shape as the existing master Loud/Warm/Clean
presets — fills the vocal track's EQ/compressor/delay/reverb sliders in
one click, drops back to "Custom" on any manual slider edit, scoped
strictly to the vocal panel's own sliders (learned from PLAN4's own
documented bug: an unscoped `.preset-chip` selector would wire multiple
preset rows to the same handler). Real, deliberate parameter choices
disclosed in docs.html, not arbitrary numbers.

New `docs.html` entries: vocal presets, upload-your-own-beat.

## Deployment

`beatstudio.lz`/`web/backend.py`/`web/process.py`/`web/page.html`/
`web/docs.html` deployed to `/opt/beatstudio/` (originals backed up
first) + service restarted. No native release needed this round — zero
new native C code (variant/key/progression pools, the fingerprint
ledger, the upload path, and the playback fixes are all pure
Larzscript/Python/JS on top of already-shipped native primitives).
