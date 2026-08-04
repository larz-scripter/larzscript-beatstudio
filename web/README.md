# web/

The live deployment behind [larzos.com/beatstudio/](https://larzos.com/beatstudio/) -
browser mic recording plus a real interactive mixer/master, wired to the
real `beatstudio.lz` pipeline. Kept here so this deployment isn't
scratchpad-only (the original `/stack/` page generator earlier in this
project had to be rebuilt from scratch once already because nothing was
persisted - not repeating that).

- **`page.html`** - the site page: an A/B before/after player for the demo
  beat, a record button (`getUserMedia`/`MediaRecorder`, your mic, nothing
  captured until you press it, up to 8 minutes), uploads to `backend.py`,
  polls for the result, then a real mixer (per-track volume/pan/mute/
  solo) and master panel (Warmth/Clarity/Air/Punch/Loudness) that POSTs
  changes to `/api/params` and re-polls. Deployed as
  `/var/www/larzos/beatstudio/index.html` on srv66.
- **`docs.html`** - plain-language explanation of every mixer/master
  control, linked from each control's "?". Deployed as
  `/var/www/larzos/beatstudio/docs/index.html`.
- **`backend.py`** - stdlib-only upload/status/params API (`POST
  /api/upload`, `GET /api/status`, `POST /api/params` - validates and
  server-side clamps every value, never trusts the page's own slider
  ranges). Runs as a systemd service (`beatstudio.service`) on
  `127.0.0.1:8478`, reverse-proxied at `/beatstudio/api/` by the site's
  Apache config (`ProxyPass /beatstudio/api http://127.0.0.1:8478/api`).
- **`process.py`** - the actual work: a cron-driven worker (every minute,
  file-locked so a slow run can't overlap the next tick) with two job
  types. `"uploaded"` (a fresh recording): decodes it with `ffmpeg`,
  then runs the full pipeline through `beatstudio.lz` -
  `init`/`step`/`pattern-step`/`arrange`/`beat-render`/`track-import`/
  `mix`/`preview`/`master` (paid, once). `"rerender_requested"` (the
  visitor changed a mixer/master control): only re-runs `mix`/`preview`/
  `remaster` (free) against the tracks the first pass already rendered.
  Both drop results as static files Apache serves directly.
- **`beatstudio.service`** - the systemd unit for `backend.py`.

## Deploying a change

```
scp beatstudio.lz srv66:/opt/beatstudio/beatstudio.lz
scp web/backend.py srv66:/opt/beatstudio/backend.py && ssh srv66 systemctl restart beatstudio
scp web/process.py srv66:/opt/beatstudio/process.py
scp web/page.html srv66:/var/www/larzos/beatstudio/index.html
scp web/docs.html srv66:/var/www/larzos/beatstudio/docs/index.html
```

`dsp`/`wav` package updates: `ssh srv66 larzscript pkg update dsp wav` (a
plain `install` can report "up to date" against a stale local
`packages-repo` checkout - `cd ~/.larzscript/packages-repo && git fetch
origin` first if a real package update isn't showing up).

The interpreter itself needs **native-v1.35.0+** (`_native_master_block`/
`_native_mix_add`/the dsp+wav packages' native buffer+streaming
contract) - `larzscript update` picks up new releases, or build from
`larz-scripter/larzscript`'s `native/larzscript.c` directly. Back up
`/usr/local/bin/larzscript` before replacing it on a box other services
also use.
