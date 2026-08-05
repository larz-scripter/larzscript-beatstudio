# PLAN9 — focused genre lineup + a signature "Larz" genre with a real, huge seed

User report (2026-08-05, same day as PLAN7/PLAN8): "the beat generation
is still repeating. i think we should create our own genre, i like
afrobeat, hiphop, r and b and trap. and remove others and create a huge
seed of our own genre which mixes the genres i gave you." Follow-up:
"our own unique genre the larz genre and put in github with
documentation."

## Why PLAN7's fix wasn't enough

PLAN7 added a variant/progression/key system, but it was still only **2
drum variants × 2 progressions × 3 keys = 12 real combinations per
genre** - small enough to genuinely repeat under regular use, exactly
what the user was still hearing. The real fix isn't a bigger fixed
number of hand-written variants (that never scales), it's a different
architecture: independent per-voice pools.

## The pool-based groove engine

Replaced PLAN7's `variant` (int, 0 or 1, picking between 2 FULLY
hand-written pattern sets) with `choose_groove(genre)` - rolls the kick
pattern, backbeat voice (clap or snare) + pattern, tom, whether an 808
layers in, hi-hat probability/timing, and open-hat accents each
**independently** from the genre's own pool (`GENRE_GROOVE_POOLS`), via
`rng.choice`. The real combinatorial space is the PRODUCT of every
pool's size, not a hand-authored count - e.g. Larz's pools alone
multiply out past a million distinct grooves from ~40 authored building
blocks. `gen_intro`/`gen_main`/`gen_fill`/`gen_chorus`/`gen_bridge` were
all rewritten to be genuinely generic across every genre now (each was
previously its own 5-6-way per-genre if/else) - they just read
whichever fields they need off the one `groove` dict `choose_groove`
built, which is rolled ONCE per `generate()` call and threaded through
all five so a song stays internally coherent (they agree on the same
kick/accent/tom, they just layer different amounts of energy on top).

Chord progressions and keys got the same pool-widening treatment (2→4
progressions and 3 keys for the 4 named genres, up to 8 progressions
and 6 keys for Larz).

## Genre lineup: focused down, plus the signature blend

Removed `lofi`, `pop`, `house` entirely (data + CLI + web UI). Renamed
`boombap` → `hiphop` (same family, clearer name). Added `rnb` from
scratch - a real neo-soul/R&B character: slower (78bpm default), a
sparser syncopated kick, hats swung onto the off-beat (`hat_start=1,
hat_step=2` - half the eligible steps of trap/hiphop's every-step
scan), either clap OR snare as the backbeat accent, and a chord pool
built ENTIRELY from major7/minor7/dom7 qualities (never a bare triad) -
the smoother harmonic color that's the genre's actual identity.

**The Larz genre** ("our own unique genre... put in github with
documentation"): the signature blend, deliberately given the BIGGEST
pool of any genre here - every list is a real union of the other 4's
own building blocks (a trap kick sits next to an afrobeat kick sits
next to a genuine hybrid nobody else uses), both accent voices, both
808 choices, a real tom option, and a chord-progression pool with 2
progressions genuinely borrowed from EACH of the other 4 genres (not
one fixed "average" harmonic recipe). Key pool spans BOTH the
minor-leaning trap/hip-hop center and the major/7th-leaning R&B/afrobeat
center - a literal "mixing the genres" implementation, not just a
bigger random number. Set as the default/featured genre in the web UI.

## Verification

Smoke-tested `generate` for all 5 genres (real, sensible groove dicts
each time) and a full init→generate→beat-render→melody-render→mix→
preview→master→report pipeline for `larz` specifically - real audio,
real chord progression, real master. Re-verified the PLAN7 fingerprint-
collision-retry mechanism against the new groove-dict-based fingerprint
(same-seed collision correctly detected, resolved in 1 retry, matching
PLAN7's own behavior). 7/7 generate-based golden tests regenerated
(5 tests that used the now-removed `pop` genre switched to `larz`,
`trap`-using tests unaffected in genre choice but re-baselined since
`gen_main`'s output format changed from `variant=N` to the full groove
dict).

New docs.html entry explaining the Larz genre and how the pool system
works generally; new README.md "Genres" section (with a real CLI
example); nav TOC in docs.html also picked up 5 entries from PLAN7/
PLAN8 that were never added there (vocal presets, upload-your-own-beat,
auto-level, volume editor, download) - a real gap found while adding
the new entry, fixed alongside it.

## Deployment

`beatstudio.lz`/`web/backend.py`/`web/process.py`/`web/page.html`/
`web/docs.html` deployed to `/opt/beatstudio/` + `/var/www/larzos/`
(originals backed up first), service restarted. `README.md` and this
file pushed to the public GitHub repo
(github.com/larz-scripter/larzscript-beatstudio) per the user's own
request. No native release needed - zero new native C code this round.
