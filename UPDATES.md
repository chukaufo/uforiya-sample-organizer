# Shipping an update

Rules and steps for releasing a new version of the Sample Organizer
without breaking anyone's library.

## What can and cannot be harmed

A producer's samples are never at risk from an update. The installer
only writes to the install directory. The index and the quarantine
staging area live in app-data — `%APPDATA%/Uforiya/` on Windows,
`~/Library/Application Support/Uforiya/` on macOS — and nothing in the
install or uninstall process touches them. The samples themselves are
nothing to do with the installer at all.

The worst realistic failure is the app refusing to open, not files
going missing.

## The four rules

**1. The index is disposable.** `library.db`'s `files` table is only
ever a description of what is on disk. If it is ever out of step with
new code, throwing it away and rescanning is safe — a 5.5 GB library
rebuilds in about a second.

**2. The quarantine tables are not.** `quarantine_batches` and
`quarantine_items` describe files that have been physically moved out
of the library. They cannot be reconstructed by scanning, because the
files are no longer where a scan would look. Losing those rows means a
producer cannot put anything back. **No update may drop, recreate, or
rebuild these two tables.**

**3. Add columns, never rewrite tables.** When new code needs a column
the old database lacks, `ALTER TABLE ... ADD COLUMN` it. SQLite treats
this as a metadata change, so it costs milliseconds regardless of row
count. `CREATE TABLE IF NOT EXISTS` does **not** add columns to a table
that already exists — relying on it is how a released build ends up
throwing SQL errors at someone who just updated.

**4. Let the scan repopulate.** A new column starts null. The scan that
already runs at launch fills it in. Nothing needs to block on it and
nothing needs to be announced, unless the migration requires a full
rehash of every file (a tier-4 style change), which is slow enough to
be worth telling the producer about.

## Release checklist

1. `package.json` — bump `version`
2. `main.py` — bump `VERSION` to match
3. If `db.py`'s schema changed: bump `SCHEMA_VERSION` and add the
   migration
4. Build on Windows: `pyinstaller worker.spec --clean --distpath dist`,
   then `npm run dist`
5. Verify the frozen worker starts:
   `./dist/worker/worker.exe --port 0 --token test` should print
   `UFORIYA_READY`
6. Install over an existing older version and confirm the library and
   any quarantine batches survived
7. Upload the installer, confirm the download link works
8. **Only then** update `latest.json`

Step 8 is last on purpose. The app sends people to the download page
the moment the JSON says a new version exists; publishing it before the
installer is live sends them to a 404.

## latest.json

Lives at `https://uforiya.com/sample-organizer/latest.json`. Read by
`main.js` on launch, which compares `version` against
`app.getVersion()`.

```json
{
  "version": "0.2.0",
  "notes": "One short line about what changed.",
  "url": "https://uforiya.com/sample-organizer"
}
```

Keep the cache lifetime on this one file short. A CDN holding the old
JSON means producers are told they are up to date for hours after a
release.

## Why updates are prompt-based

Windows will not let an unsigned application replace its own binaries,
so silent background updating is not available until the app is code
signed. Until then the app shows a banner, the producer downloads the
installer, and runs it. This works fine — it is just not invisible.

Revisit if and when a certificate is bought.
