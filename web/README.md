# web/

The live deployment behind [larzos.com/beatstudio/](https://larzos.com/beatstudio/) -
browser mic recording, wired to the real `beatstudio.lz` pipeline. Kept
here so this deployment isn't scratchpad-only (the original `/stack/` page
generator earlier in this project had to be rebuilt from scratch once
already because nothing was persisted - not repeating that).

- **`page.html`** - the site page: an A/B before/after player for the demo
  beat, a record button (`getUserMedia`/`MediaRecorder`, your mic, nothing
  captured until you press it), uploads to `backend.py`, polls for the
  result. Deployed as `/var/www/larzos/beatstudio/index.html` on srv66.
- **`backend.py`** - stdlib-only upload/status API (`POST /api/upload`,
  `GET /api/status`). Runs as a systemd service (`beatstudio.service`) on
  `127.0.0.1:8478`, reverse-proxied at `/beatstudio/api/` by the site's
  Apache config (`ProxyPass /beatstudio/api http://127.0.0.1:8478/api`).
- **`process.py`** - the actual work: a cron-driven worker (every minute,
  file-locked so a slow run can't overlap the next tick) that picks up an
  uploaded recording, decodes it with `ffmpeg` (the one non-Larzscript
  step - real audio capture needs an OS driver no interpreted language
  has bindings for), and runs it through `beatstudio.lz` for real -
  `init`/`step`/`pattern-step`/`arrange`/`beat-render`/`track-import`/
  `mix`/`preview`/`master` - dropping the results as static files Apache
  serves directly.
- **`beatstudio.service`** - the systemd unit for `backend.py`.

## Deploying a change

```
scp beatstudio.lz srv66:/opt/beatstudio/beatstudio.lz
scp web/backend.py srv66:/opt/beatstudio/backend.py && ssh srv66 systemctl restart beatstudio
scp web/process.py srv66:/opt/beatstudio/process.py
scp web/page.html srv66:/var/www/larzos/beatstudio/index.html
```

`dsp`/`wav` package updates: `ssh srv66 larzscript pkg install dsp wav`.
