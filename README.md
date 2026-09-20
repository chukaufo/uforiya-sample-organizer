# Uforiya Sample Organizer

Finds duplicate samples across a producer's drum kit library, and
checks newly downloaded kits against what they already own.

Local only. No account, no backend, no cloud. Nothing leaves the
machine.

## Status

Milestone 1 of 8. The worker walks a library, maintains a SQLite
index, and finds byte-identical duplicates through three tiers. No UI
yet — the worker is driven by HTTP.

| # | Milestone | State |
|---|-----------|-------|
| 1 | Walk + index | Done |
| 2 | Tiers 1–3 → `GET /duplicates` | Tiers done, route pending |
| 3 | Tier 4 (re-encoded dupes) + waveform peaks | Not started |
| 4 | Quarantine + restore | Not started |
| 5 | Windows CI | Not started |
| 6 | Electron shell | Not started |
| 7 | Duplicate review list | Not started |
| 8 | Kit compare | Not started |

## Architecture

Python owns all the work — scanning, hashing, decoding, grouping,
quarantine, the index. The desktop shell is a window that renders
results and sends commands; it holds no logic and no state worth
keeping.

The worker runs as a child process of that shell: it binds
`127.0.0.1` on an OS-assigned port, prints `UFORIYA_READY {"port": N}`
on stdout for its parent to read, and requires a bearer token its
parent generated. HTTP here is a messaging format between two
processes on one machine — there is no browser and no network.

## Dedupe tiers

Cheapest first. Each tier only runs on what survived the one before,
which is what keeps a 50,000-file library finishing in minutes.

| Tier | Cost | Catches |
|------|------|---------|
| Size bucket | Free, from the walk | Nothing alone — eliminates most of the library |
| Partial hash, 128KB | ~0.1 ms | Rules out same-size-different-file |
| Full SHA-256 | ~5 ms/MB | Byte-identical copies |
| Decoded PCM hash | 30–80 ms | Same audio, different container or bitrate |

Only 100% matches count as duplicates. No spectral fingerprinting, no
similarity thresholds, no BPM or key detection.

## Safety

The app never deletes a file. Approved removals move to a quarantine
folder in app-data and are restorable; permanent deletion is a
separate, deliberate second action. The worker rejects any selection
that would leave zero copies of a sample — enforced in Python, not in
the UI.

## Running it

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

UFORIYA_DEV_TOKEN=dev python main.py --port 8787
```

Then:

```bash
curl -s localhost:8787/health

curl -s -X POST localhost:8787/library/roots \
  -H "Authorization: Bearer dev" \
  -H "Content-Type: application/json" \
  -d '{"path":"/path/to/your/samples"}'

curl -N -s "localhost:8787/jobs/<job_id>/events?token=dev"
```

Every route takes `Authorization: Bearer <token>` except `/health`.
The SSE route takes the token as a query parameter instead, because
browser `EventSource` cannot set request headers.

In development, run `uvicorn` directly with a fixed port and token.
The frozen binary path (`--port 0`, token on argv) is what ships.

## Index location

`%APPDATA%/Uforiya/library.db` on Windows,
`~/Library/Application Support/Uforiya/library.db` on macOS. Never
inside the sample library — moving your samples should not move the
index.

## Platform

Windows ships first. Development happens on macOS, so a Mac build
falls out of the same codebase and is the daily test target.
PyInstaller does not cross-compile: Windows builds come from a
`windows-latest` CI runner.
