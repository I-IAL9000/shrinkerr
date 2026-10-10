# Demo

A demo Shrinkerr shows the app without a library of your own: a made-up
collection of films and TV episodes, with a made-up history of conversions,
in a copy of Shrinkerr where nothing can be changed. It's for letting people
look around before they install it.

## Running one

```bash
docker compose -f docker-compose.demo.yml up -d
```

Then open <http://localhost:6680>. The compose file runs the normal image
with `SHRINKERR_DEMO=1`, and keeps its data in memory (tmpfs), so every
restart starts the demo afresh. It needs no media folders, GPU or API keys.

To share it, put it behind your reverse proxy with HTTPS, like any other
Shrinkerr ([Installation → Reverse proxy](installation.md#reverse-proxy-setups)).

## What's different in demo mode

- **Nobody signs in**, and a banner says it's a demo.
- **Nothing can be changed.** Requests that would change something (saving
  settings, queueing, starting the queue, editing rules…) are refused with
  "This is a demo: changes are turned off". Reading works, and so do the
  requests that only compute something: the Add to Queue estimate, the rule
  tester, Scanner search and rename previews.
- **The library is generated** on the first start: about 140 films and 160
  episodes with made-up titles, their audio and subtitle tracks, about 90
  finished conversions, 24 queued jobs and one failed one. There are no
  files behind them.
- **The watcher and the queue don't run**, and scans can't be started. The
  update check and the weekly database backup still run, as on any install.
  Posters aren't fetched, so titles show their first letter.

`SHRINKERR_DEMO=1` turns all of this on. Don't set it on an install you use:
the demo library only fills an empty database, so it would show your own
library to anyone, without signing in, and stop the queue.
