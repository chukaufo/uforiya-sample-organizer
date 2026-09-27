# Uforiya Sample Organizer

Finds duplicate samples across a producer's drum kit library.

Local only. No account, no backend, no cloud. Nothing leaves the
machine.

Windows installer: [latest release](https://github.com/chukaufo/uforiya-sample-organizer/releases/latest)
· [uforiya.com/sample-organizer](https://uforiya.com/sample-organizer)

## Status

0.1.0, shipped for Windows. macOS is next.

## Architecture

Python owns all the work — scanning, hashing, grouping, quarantine,
the index. The Electron shell is a window that renders results and
sends commands; it holds no logic and no state worth keeping.

The worker runs as a child process of that shell: it binds
`127.0.0.1` on an OS-assigned port, prints `UFORIYA_READY {"port": N}`
on stdout for its parent to read, and requires a bearer token its
parent generated. HTTP here is a messaging format between two
processes on one machine — there is no browser and no network.

FFmpeg ships beside the worker, for previewing formats Chromium
cannot decode natively (AIFF above all).

## Dedupe tiers

Cheapest first. Each tier only runs on what survived the one before,
which is what keeps a large library finishing in seconds.

| Tier | Cost | Catches |
|------|------|---------|
| Size bucket | Free, from the walk | Nothing alone — eliminates most of the library |
| Partial hash, 128KB | ~0.1 ms | Rules out same-size-different-file |
| Full SHA-256 | ~5 ms/MB | Byte-identical copies |

Only 100% matches count as duplicates. No spectral fingerprinting, no
similarity thresholds.

Duplicates are found across every registered folder, not within each
one — the same sample in two different kits on two different drives is
one group.

## Safety

The app never deletes a file without being told to, twice.

Approved removals move to a quarantine folder in app-data and are
restorable, whole batches or one file at a time. Permanent deletion is
a separate, deliberate action.

The worker rejects any selection that would leave zero copies of a
sample — enforced in Python, not in the UI.

Every quarantine move is journalled to disk before the file moves, and
its index row is written the moment the move succeeds. If the app is
killed mid-operation, a recovery pass on the next launch adopts any
files it finds in quarantine that the index lost track of, and tells
the producer what happened. A crash cannot lose a sample.

## Audio formats

WAV, AIFF, MP3, M4A, AAC, FLAC, OGG, Opus, WMA and others. Everything
else in a folder is ignored and never touched — project files,
presets, MIDI, artwork.

## Running it from source

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

npm install
npm start
```

The Electron shell starts the worker itself. To drive the worker alone:

```bash
UFORIYA_DEV_TOKEN=dev python main.py --port 8787
curl -s localhost:8787/health
```

Every route takes `Authorization: Bearer <token>` except `/health`.
The SSE and audio routes take the token as a query parameter instead,
because browser `EventSource` and `<audio>` cannot set request headers.

## Building

FFmpeg must be on PATH — the spec fails the build without it.

```bash
pip install pyinstaller
python -m PyInstaller worker.spec
npm run dist
```

`python -m PyInstaller` rather than the bare command, so it uses the
active virtualenv rather than whatever is first on PATH.

PyInstaller does not cross-compile: the Windows build comes from a
Windows machine, the macOS build from a Mac.

See `UPDATES.md` for the release checklist.

## Index location

`%APPDATA%/Uforiya/library.db` on Windows,
`~/Library/Application Support/Uforiya/library.db` on macOS. Never
inside the sample library — moving your samples should not move the
index. An installer never touches it.
