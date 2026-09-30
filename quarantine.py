# Quarantine: the only code in this app that moves a producer's files.
#
# Nothing is ever deleted here. Approved files are moved to a staging
# folder in app-data, the index records where each came from, and a
# journal line is written beside them before each move — so files moved
# before a crash are recoverable on the next launch. Permanent deletion
# is a separate, later action the user takes deliberately.
#
# Three reasons this is the right model rather than a recycle-bin send
# or a straight unlink. A false positive costs a sample that cannot be
# got back. The Windows Recycle Bin is unreliable for programmatic
# sends and silently skips files past the bin's size limit. And the
# first time someone points this at a 50,000-file library they will
# not trust it — a visible, reversible staging area is what earns that
# trust.
#
# Every guard below runs in this module rather than in the UI. A bug
# in a renderer must not be able to wipe the last copy of a sound.
import json
import os
import shutil
import time
from stat import S_IWRITE
from typing import Optional

import db
import hashing
from platform_paths import IS_WINDOWS, normalize_path, quarantine_dir, restored_dir

# Whole-file manifest written by builds up to 0.1.0. Still read during
# recovery so a batch made by an older build is not stranded.
MANIFEST_NAME = "manifest.json"

# Append-only journal, one JSON object per line. Written a line at a
# time rather than rewritten whole, so the cost per file is a short
# append instead of re-serialising the entire batch, and a crash
# mid-append costs one unparseable line rather than the whole file.
JOURNAL_NAME = "manifest.jsonl"


class QuarantineError(Exception):
    """Refusal. The message is shown to the user, so it names what was
    wrong and which file caused it."""


# ── Guards ───────────────────────────────────────────────────────────────────

def _verify_group_survives(file_ids: list) -> None:
    """
    Refuses any selection that would leave a group with no copies.

    This is the single most important check in the app. It lives here,
    not in the renderer, because a UI bug — a bad select-all, an
    off-by-one in a virtualised list — must not be able to destroy the
    only copy of a sample.

    Checks both hash columns. A file can belong to an exact group and
    a re-encoded group at once; emptying either is refused.
    """
    selected = set(file_ids)

    for column in ("sha256", "pcm_hash"):
        rows = db.get_conn().execute(
            f"""
            SELECT f.{column} AS hash_value, f.id, f.rel_path
            FROM files f
            WHERE f.state = 'present'
              AND f.{column} IS NOT NULL
              AND f.{column} IN (
                  SELECT {column} FROM files
                  WHERE id IN ({','.join('?' * len(selected))})
              )
            """,
            tuple(selected),
        ).fetchall()

        groups: dict = {}
        for row in rows:
            groups.setdefault(row["hash_value"], []).append(row)

        for hash_value, members in groups.items():
            remaining = [m for m in members if m["id"] not in selected]
            if not remaining:
                example = members[0]["rel_path"]
                raise QuarantineError(
                    "Refused: this would remove every copy of a sample "
                    f"({example}). At least one copy must stay."
                )


def _verify_unchanged(row: dict, abs_path: str) -> None:
    """
    Refuses a file whose size or mtime differs from the indexed row.

    The scan that produced this decision looked at particular bytes. If
    the file changed since, the decision was made about a different
    file, and moving it would be acting on stale information.
    """
    try:
        stat = os.stat(abs_path)
    except OSError as exc:
        raise QuarantineError(f"Cannot read {row['rel_path']}: {exc}")

    if stat.st_size != row["size"] or stat.st_mtime_ns != row["mtime_ns"]:
        raise QuarantineError(
            f"Refused: {row['rel_path']} changed since the last scan. "
            "Rescan before removing it."
        )


# ── Moving ───────────────────────────────────────────────────────────────────

def _long_path(path: str) -> str:
    """
    Windows form of a path that may exceed 260 characters.

    Quarantine nests a kit's full relative path under app-data, so a path
    that was fine in the producer's library can cross MAX_PATH once it is
    inside a batch folder. The \\\\?\\ prefix opts out of that limit, and
    is only meaningful on Windows with a drive-letter path.
    """
    if not IS_WINDOWS:
        return path

    full = os.path.abspath(path)
    if full.startswith("\\\\"):       # already prefixed, or a UNC share
        return full
    return "\\\\?\\" + full.replace("/", "\\")

def _move_file(source: str, destination: str) -> None:
    """
    Moves one file, verifying the copy when a rename is not possible.

    os.rename is atomic within a volume and is what runs in the normal
    case — the file is never in two places or neither. Across volumes
    it raises, and the fallback is copy, verify by hash, then delete
    the source. Deleting before verifying is the one way this app
    could actually lose a sample, so the verification is not optional.
    """
    # Both ends go through _long_path: the destination is nested deeper
    # than the source, so a path that was fine in the producer's library
    # can cross MAX_PATH once it is inside a batch folder. The error
    # message below still uses the original, because \\?\C:\... is not a
    # path anyone wants to read.
    src = _long_path(source)
    dst = _long_path(destination)

    os.makedirs(os.path.dirname(dst), exist_ok=True)

    try:
        os.rename(src, dst)
        return
    except OSError:
        pass  # Cross-volume, or a filesystem that refuses the rename.

    shutil.copy2(src, dst)

    if not hashing.verify_copy(src, dst):
        # Leave the source untouched and clean up the bad copy.
        try:
            os.remove(dst)
        except OSError:
            pass
        raise QuarantineError(
            f"Refused: copy of {os.path.basename(source)} did not verify. "
            "Original left in place."
        )

    os.remove(src)


def _journal_path(batch_dir: str) -> str:
    return os.path.join(batch_dir, JOURNAL_NAME)


def _journal_batch(batch_dir: str, batch_id: int, created_at: int) -> None:
    """Opening line of a batch's journal: what it is and when it started."""
    _journal_write(batch_dir, {
        "type": "batch",
        "batch_id": batch_id,
        "created_at": created_at,
    })


def _journal_item(batch_dir: str, item: dict) -> None:
    """One file's line. Everything a restore needs, and nothing else."""
    _journal_write(batch_dir, {
        "type": "item",
        "original_path": item["original_path"],
        "quarantine_path": item["quarantine_path"],
        "sha256": item["sha256"],
        "size": item["size"],
    })


def _journal_write(batch_dir: str, entry: dict) -> None:
    """
    Appends one line and flushes it to the OS.

    flush without fsync on purpose. Flushing puts the line beyond this
    process, which is what a crash or a force-quit costs; fsync would
    additionally survive the machine losing power, at the price of a
    disk sync per file. The index makes the same trade
    (synchronous=NORMAL in db.py), and matching it keeps the two
    records equally durable rather than one waiting on the other.
    """
    with open(_journal_path(batch_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
        f.flush()


def read_journal(batch_dir: str) -> dict:
    """
    Everything a batch's journal claims, as {created_at, items}.

    Lines that do not parse are skipped rather than fatal — a crash
    during an append leaves a truncated final line, and one lost line
    must not cost the other five hundred. Falls back to the old
    whole-file manifest for batches written by earlier builds.
    """
    created_at = None
    items = []
    path = _journal_path(batch_dir)

    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") == "batch":
                    created_at = entry.get("created_at")
                elif entry.get("quarantine_path"):
                    items.append(entry)
        return {"created_at": created_at, "items": items}

    legacy = os.path.join(batch_dir, MANIFEST_NAME)
    if os.path.isfile(legacy):
        try:
            with open(legacy, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                "created_at": data.get("created_at"),
                "items": data.get("items", []),
            }
        except (ValueError, OSError):
            pass

    return {"created_at": None, "items": []}


# ── Restore destinations ─────────────────────────────────────────────────────

def _matching_root(original_path: str, roots: list) -> Optional[dict]:
    """
    The registered root a quarantined file came from, longest match first.

    Longest wins because roots can nest — a producer may have added both
    their samples folder and one kit inside it, and the kit is the more
    specific answer.
    """
    best = None
    for root in roots:
        prefix = root["path"].rstrip("/") + "/"
        if original_path.startswith(prefix):
            if best is None or len(root["path"]) > len(best["path"]):
                best = root
    return best


def _restore_destination(original_path: str, roots: list) -> tuple:
    """
    Where a file should go, and whether that is its original home.

    Returns (destination, relocated). Relocated means the original
    folder no longer exists and the file is going somewhere else — which
    the caller must report, and must not record as a restore to the
    index.
    """
    if os.path.isdir(os.path.dirname(original_path)):
        return original_path, False

    root = _matching_root(original_path, roots)

    if root:
        base = restored_dir(near=root["path"])
        # Path relative to the root is kept, so a restored file still
        # says which kit it belonged to.
        relative = original_path[len(root["path"].rstrip("/")) + 1:]
    else:
        base = restored_dir()
        relative = os.path.basename(original_path)

    return normalize_path(os.path.join(str(base), relative)), True


# ── Public API ───────────────────────────────────────────────────────────────
# What the last recovery pass adopted, for the UI to report once. Reset
# on every launch, since it describes this launch.
LAST_RECOVERY = {"batches": 0, "files": 0}


def _index_file_id(original_path: str, roots: list) -> int:
    """
    The index row a quarantined file belongs to, or 0 if there isn't one.

    Journals store paths, not row ids, because a rebuilt index has
    different ids and adopting a stale one would flip the state of an
    unrelated file. 0 means 'no row' — the file is still restorable to
    its original path, the index simply has nothing to update.
    """
    root = _matching_root(original_path, roots)
    if root is None:
        return 0

    relative = original_path[len(root["path"].rstrip("/")) + 1:]
    row = db.get_conn().execute(
        "SELECT id FROM files WHERE root_id = ? AND rel_path = ?",
        (root["id"], relative),
    ).fetchone()
    return row["id"] if row else 0


def recover_orphan_batches() -> dict:
    """
    Adopts quarantined files the index does not know about.

    The case this exists for: the app was killed partway through a
    move, or library.db was lost, and there are real files sitting in a
    batch folder that nothing in the app can see. Before this ran, they
    were invisible — a producer would open Quarantine, find it empty,
    and conclude their samples were gone.

    Nothing here moves or deletes a file. It reads each batch's
    journal, confirms the file is actually where the journal says, and
    writes the missing index rows so Put back works normally.

    Purged batches are skipped: their rows say deliberately deleted,
    and re-adopting leftovers would undo a decision the producer made.
    """
    base = str(quarantine_dir())
    roots = db.list_roots()
    batches = 0
    files = 0

    for name in sorted(os.listdir(base)):
        batch_dir = os.path.join(base, name)
        if not name.isdigit() or not os.path.isdir(batch_dir):
            continue

        batch_id = int(name)
        batch = db.get_conn().execute(
            "SELECT id, purged_at FROM quarantine_batches WHERE id = ?",
            (batch_id,),
        ).fetchone()
        if batch and batch["purged_at"]:
            continue

        journal = read_journal(batch_dir)
        if not journal["items"]:
            continue

        known = {
            row["quarantine_path"]
            for row in db.get_conn().execute(
                "SELECT quarantine_path FROM quarantine_items WHERE batch_id = ?",
                (batch_id,),
            ).fetchall()
        }

        # Only entries whose file is genuinely there. A journal line is
        # a claim, not proof — the move it describes may never have
        # happened, or the file may have been restored since.
        # _long_path on the isfile check: a quarantine path over MAX_PATH
        # would report as absent, recovery would conclude the move never
        # happened, and the file would stay stranded — which is the one
        # outcome this function exists to prevent.
        adopt = [
            entry for entry in journal["items"]
            if entry["quarantine_path"] not in known
            and os.path.isfile(_long_path(entry["quarantine_path"]))
        ]
        if not adopt:
            continue

        with db._write_lock:
            conn = db.get_conn()

            if batch is None:
                conn.execute(
                    "INSERT INTO quarantine_batches (id, created_at, file_count, bytes) "
                    "VALUES (?, ?, 0, 0)",
                    (batch_id,
                     journal["created_at"] or int(os.path.getmtime(batch_dir))),
                )

            for entry in adopt:
                file_id = _index_file_id(entry["original_path"], roots)
                conn.execute(
                    "INSERT INTO quarantine_items "
                    "(batch_id, file_id, original_path, quarantine_path, sha256, size) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (batch_id, file_id, entry["original_path"],
                     entry["quarantine_path"], entry.get("sha256"), entry["size"]),
                )
                if file_id:
                    conn.execute(
                        "UPDATE files SET state = 'quarantined' WHERE id = ?",
                        (file_id,),
                    )

            conn.execute(
                "UPDATE quarantine_batches SET file_count = file_count + ?, "
                "bytes = bytes + ? WHERE id = ?",
                (len(adopt), sum(e["size"] for e in adopt), batch_id),
            )
            conn.commit()

        batches += 1
        files += len(adopt)

    LAST_RECOVERY["batches"] = batches
    LAST_RECOVERY["files"] = files
    return dict(LAST_RECOVERY)

def reconcile_missing_batches() -> int:
    """
    Closes out batches whose folder is gone from disk.

    The case: purge failed partway, the producer deleted the rest by hand
    in Explorer, and the app still lists a batch that no longer exists.
    Mirrors purge_batch's bookkeeping. Returns how many were closed.
    """
    base = str(quarantine_dir())
    rows = db.get_conn().execute(
        "SELECT id FROM quarantine_batches WHERE purged_at IS NULL"
    ).fetchall()

    closed = 0
    for row in rows:
        if os.path.isdir(os.path.join(base, str(row["id"]))):
            continue
        with db._write_lock:
            conn = db.get_conn()
            conn.execute(
                "DELETE FROM quarantine_items WHERE batch_id = ?", (row["id"],)
            )
            conn.execute(
                "UPDATE quarantine_batches SET purged_at = ? WHERE id = ?",
                (int(time.time()), row["id"]),
            )
            conn.commit()
        closed += 1
    return closed

def quarantine_files(file_ids: list) -> dict:
    """
    Moves the selected files into a new quarantine batch.

    Guards run before anything moves: the last-copy check across the
    whole selection, then a per-file staleness check. A refusal means
    nothing was touched.

    Once moving begins, a failure partway leaves the batch with
    whatever succeeded — recorded, restorable, and reported. Rolling
    back would mean moving files a second time to undo a move that
    already failed once, which is more risk than it removes.
    """
    if not file_ids:
        raise QuarantineError("No files selected")

    rows = db.get_conn().execute(
        f"""
        SELECT f.id, f.rel_path, f.size, f.mtime_ns, f.sha256,
               r.path AS root_path
        FROM files f
        JOIN roots r ON r.id = f.root_id
        WHERE f.id IN ({','.join('?' * len(file_ids))})
          AND f.state = 'present'
        """,
        tuple(file_ids),
    ).fetchall()

    found = {row["id"] for row in rows}
    missing = set(file_ids) - found
    if missing:
        raise QuarantineError(
            f"{len(missing)} selected file(s) are no longer in the index. Rescan."
        )

    _verify_group_survives(list(found))

    prepared = []
    for row in rows:
        # Normalised, because everything downstream — the root match on
        # restore, the recovery pass — compares these against roots
        # stored with forward slashes. os.path.join on Windows would
        # hand back a mixed-separator path that matches nothing.
        abs_path = normalize_path(os.path.join(row["root_path"], row["rel_path"]))
        _verify_unchanged(row, abs_path)
        prepared.append((row, abs_path))

    # Nothing has moved yet. Past this point, files are on the move.
    now = int(time.time())
    with db._write_lock:
        conn = db.get_conn()
        cur = conn.execute(
            "INSERT INTO quarantine_batches (created_at, file_count, bytes) "
            "VALUES (?, 0, 0)",
            (now,),
        )
        batch_id = cur.lastrowid
        conn.commit()

    batch_dir = os.path.join(str(quarantine_dir()), str(batch_id))
    os.makedirs(batch_dir, exist_ok=True)
    _journal_batch(batch_dir, batch_id, now)

    moved = []
    failures = []

    for row, abs_path in prepared:
        # Relative path preserved inside the batch, so two files named
        # Clap.wav from different kits cannot collide, and a restore by
        # hand is legible.
        destination = os.path.join(batch_dir, row["rel_path"])

        item = {
            "file_id": row["id"],
            "original_path": abs_path,
            "quarantine_path": destination,
            "sha256": row["sha256"],
            "size": row["size"],
        }

        # Written before the move rather than after. If the app dies
        # between the move landing and the database row being written,
        # this line is the only thing that knows the file left its
        # folder — and recovery on the next launch reads it, confirms
        # the file really is in the batch directory, and puts it back
        # into quarantine properly.
        #
        # A line whose move never happened costs nothing: recovery finds
        # no file at quarantine_path and ignores it.
        _journal_item(batch_dir, item)

        try:
            _move_file(abs_path, destination)
        except (QuarantineError, OSError) as exc:
            failures.append({"path": row["rel_path"], "error": str(exc)})
            continue

        moved.append(item)

        # Recorded the instant this one file's move succeeds — not
        # batched until the whole loop finishes. A kill between any two
        # files must never leave a moved file untracked: on disk in
        # quarantine, but with no DB row and no manifest entry pointing
        # at it.
        with db._write_lock:
            conn = db.get_conn()
            conn.execute(
                "INSERT INTO quarantine_items "
                "(batch_id, file_id, original_path, quarantine_path, sha256, size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (batch_id, item["file_id"], item["original_path"],
                 item["quarantine_path"], item["sha256"], item["size"]),
            )
            conn.execute(
                "UPDATE files SET state = 'quarantined' WHERE id = ?",
                (item["file_id"],),
            )
            conn.execute(
                "UPDATE quarantine_batches SET file_count = file_count + 1, "
                "bytes = bytes + ? WHERE id = ?",
                (item["size"], batch_id),
            )
            conn.commit()

    total_bytes = sum(item["size"] for item in moved)

    return {
        "batch_id": batch_id,
        "moved": len(moved),
        "bytes": total_bytes,
        "failures": failures,
    }


def list_batches() -> list:
    """Batches with counts and reclaimable bytes, newest first."""
    rows = db.get_conn().execute(
        """
        SELECT id, created_at, file_count, bytes, purged_at
        FROM quarantine_batches
        ORDER BY created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_batch(batch_id: int) -> Optional[dict]:
    """One batch and its items. Read-only."""
    batch = db.get_conn().execute(
        "SELECT id, created_at, file_count, bytes, purged_at "
        "FROM quarantine_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None

    items = db.get_conn().execute(
        "SELECT id, file_id, original_path, quarantine_path, sha256, size "
        "FROM quarantine_items WHERE batch_id = ? ORDER BY original_path",
        (batch_id,),
    ).fetchall()

    result = dict(batch)
    result["items"] = [dict(row) for row in items]
    return result

def batch_folder(batch_id: Optional[int] = None) -> str:
    """
    The folder a producer can open in their file manager.

    Resolved here from an integer id so the renderer never supplies a
    path. With no id, the quarantine root, which is also the way in
    when a batch was marked purged but its files are still on disk.
    """
    root = str(quarantine_dir())
    if batch_id is None:
        return root

    folder = os.path.join(root, str(int(batch_id)))
    if not os.path.isdir(folder):
        raise QuarantineError("That batch's folder no longer exists on disk.")
    return folder

def _restore_rows(batch_id: int, items: list, roots: list) -> dict:
    """
    Puts a set of quarantine rows back, and clears them from the index.

    Shared by whole-batch restore and single-file restore, because the two
    differ only in which rows they hand over. Every guard — the
    already-exists check, the relocation fallback, the collision suffix —
    must behave identically whether a producer restores one sample or five
    hundred.
    """
    restored = []     # back at their original paths
    relocated = []    # out of quarantine, but somewhere else
    failures = []

    for item in items:
        if os.path.exists(item["original_path"]):
            failures.append({
                "path": item["original_path"],
                "error": "A file already exists at this path; left untouched.",
            })
            continue

        try:
            destination, moved_elsewhere = _restore_destination(
                item["original_path"], roots
            )
        except OSError as exc:
            failures.append({"path": item["original_path"], "error": str(exc)})
            continue

        # A name collision in the restore folder is possible — two kits
        # can hold the same relative path. Suffix rather than overwrite.
        if moved_elsewhere and os.path.exists(destination):
            stem, ext = os.path.splitext(destination)
            n = 2
            while os.path.exists(f"{stem} ({n}){ext}"):
                n += 1
            destination = f"{stem} ({n}){ext}"

        try:
            _move_file(item["quarantine_path"], destination)
        except (QuarantineError, OSError) as exc:
            failures.append({"path": item["original_path"], "error": str(exc)})
            continue

        if moved_elsewhere:
            relocated.append({**dict(item), "restored_to": destination})
        else:
            restored.append(dict(item))

    cleared = restored + relocated

    if cleared:
        with db._write_lock:
            conn = db.get_conn()
            conn.executemany(
                "DELETE FROM quarantine_items WHERE id = ?",
                [(item["id"],) for item in cleared],
            )
            # Back to 'present' only for files actually at their indexed
            # location. Their hashes are still valid — the bytes never
            # changed, only the location.
            #
            # file_id 0 is a recovered item with no matching index row.
            # The file still goes back to its original path; there is
            # simply no row to update, and a later scan picks it up.
            if restored:
                conn.executemany(
                    "UPDATE files SET state = 'present' WHERE id = ?",
                    [(item["file_id"],) for item in restored if item["file_id"]],
                )
            if relocated:
                conn.executemany(
                    "UPDATE files SET state = 'missing' WHERE id = ?",
                    [(item["file_id"],) for item in relocated if item["file_id"]],
                )
            conn.execute(
                "UPDATE quarantine_batches SET file_count = file_count - ?, "
                "bytes = bytes - ? WHERE id = ?",
                (
                    len(cleared),
                    sum(item["size"] for item in cleared),
                    batch_id,
                ),
            )
            conn.commit()

    return {
        "batch_id": batch_id,
        "restored": len(restored),
        "relocated": len(relocated),
        # One path is enough for the UI to point at; the rest are siblings.
        "relocated_to": relocated[0]["restored_to"] if relocated else None,
        "failures": failures,
    }


def restore_item(item_id: int) -> dict:
    """
    Puts one quarantined file back, leaving the rest of its batch alone.

    A producer scanning a batch of five hundred usually wants three of
    them back, not all of them — and having to restore the whole batch
    and re-select the other four hundred and ninety-seven is not an
    answer.

    The batch row stays even when this empties it. Purging is the one
    destructive thing in this app and it is never a side effect.
    """
    row = db.get_conn().execute(
        "SELECT i.id, i.batch_id, i.file_id, i.original_path, "
        "       i.quarantine_path, i.size, b.purged_at "
        "FROM quarantine_items i "
        "JOIN quarantine_batches b ON b.id = i.batch_id "
        "WHERE i.id = ?",
        (item_id,),
    ).fetchone()

    if row is None:
        raise QuarantineError("That file is no longer in quarantine")
    if row["purged_at"]:
        raise QuarantineError("That batch was permanently deleted")

    return _restore_rows(row["batch_id"], [row], db.list_roots())

def restore_batch(batch_id: int) -> dict:
    """
    Puts every file in a batch back where it came from.

    original_path is stored absolute precisely for this: a restore must
    work even if the app was reinstalled, the root was removed from the
    index, or the library moved in between.

    A file already present at the original path is skipped rather than
    overwritten. The producer may have re-downloaded the kit, and
    clobbering their current file to restore an older copy of it would
    be its own kind of loss.

    A file whose original folder no longer exists is not put back into a
    folder recreated for the purpose — a producer who reorganised their
    library meant to. It goes to a restore folder beside the library
    instead, and is reported as relocated so they know where to look.

    Relocated files leave quarantine but do not return to the index as
    present: the index knows them by root and relative path, and that is
    no longer where they are. They are marked missing, which is the
    truth, and a later scan picks them up if they are ever moved into a
    library folder.
    """
    batch = db.get_conn().execute(
        "SELECT id, purged_at FROM quarantine_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise QuarantineError(f"Batch {batch_id} not found")
    if batch["purged_at"]:
        raise QuarantineError(f"Batch {batch_id} was permanently deleted")

    items = db.get_conn().execute(
        "SELECT id, file_id, original_path, quarantine_path, size "
        "FROM quarantine_items WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()

    roots = db.list_roots()

    return _restore_rows(batch_id, items, roots)
    
def purge_item(item_id: int) -> dict:
    """
    Permanently deletes one quarantined file, leaving its batch alone.

    The per-file counterpart to purge_batch, and the same rule applies:
    this is the only thing in the app that destroys a producer's data,
    and it only ever happens because they chose it for this file.

    The journal line stays. It is an append-only record of what was
    moved, and rewriting history to hide a deletion would make the one
    file that recovery must never adopt look like one it should — the
    file is gone from disk, so recovery's own isfile check already
    refuses it.
    """
    row = db.get_conn().execute(
        "SELECT i.id, i.batch_id, i.file_id, i.quarantine_path, i.size, "
        "       b.purged_at "
        "FROM quarantine_items i "
        "JOIN quarantine_batches b ON b.id = i.batch_id "
        "WHERE i.id = ?",
        (item_id,),
    ).fetchone()

    if row is None:
        raise QuarantineError("That file is no longer in quarantine")
    if row["purged_at"]:
        raise QuarantineError("That batch was permanently deleted")

    target = _long_path(row["quarantine_path"])
    try:
        try:
            os.remove(target)
        except PermissionError:
            # The read-only attribute is the common Windows refusal.
            # Clear it and retry once; other platforms just retry.
            if IS_WINDOWS:
                os.chmod(target, S_IWRITE)
            os.remove(target)
    except FileNotFoundError:
        pass   # Already gone. The row is what is left to clean up.
    except OSError as exc:
        raise QuarantineError(f"Could not delete that file: {exc}")
    with db._write_lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM quarantine_items WHERE id = ?", (item_id,))
        # The index row goes too, rather than becoming 'missing': a
        # later scan would otherwise keep looking for a file the
        # producer deliberately destroyed.
        if row["file_id"]:
            conn.execute(
                "DELETE FROM files WHERE id = ?",
                (row["file_id"],),
            )
        conn.execute(
            "UPDATE quarantine_batches SET file_count = file_count - 1, "
            "bytes = bytes - ? WHERE id = ?",
            (row["size"], row["batch_id"]),
        )
        conn.commit()

    return {
        "item_id": item_id,
        "batch_id": row["batch_id"],
        "purged": True,
        "bytes_freed": row["size"],
    }

def _is_gone(path: str) -> bool:
    """
    True only when the file is definitely absent.

    FileNotFoundError specifically, not any OSError: a file inside a
    folder the app cannot read reports as inaccessible, and treating
    that as 'deleted' would drop the row for a file still on disk.
    """
    try:
        os.stat(_long_path(path))
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _drop_deleted_items(batch_id: int) -> None:
    """
    After a partly failed purge, forgets the items that really were
    deleted so the batch lists only what is left. Same bookkeeping as
    purge_item: the row, the index entry, and the batch totals.
    """
    rows = db.get_conn().execute(
        "SELECT id, file_id, quarantine_path, size "
        "FROM quarantine_items WHERE batch_id = ?",
        (batch_id,),
    ).fetchall()

    gone = [r for r in rows if _is_gone(r["quarantine_path"])]
    if not gone:
        return

    with db._write_lock:
        conn = db.get_conn()
        conn.executemany(
            "DELETE FROM quarantine_items WHERE id = ?",
            [(r["id"],) for r in gone],
        )
        index_rows = [(r["file_id"],) for r in gone if r["file_id"]]
        if index_rows:
            conn.executemany("DELETE FROM files WHERE id = ?", index_rows)
        conn.execute(
            "UPDATE quarantine_batches SET file_count = file_count - ?, "
            "bytes = bytes - ? WHERE id = ?",
            (len(gone), sum(r["size"] for r in gone), batch_id),
        )
        conn.commit()

def purge_batch(batch_id: int) -> dict:
    """
    Permanently deletes a batch. The only destructive operation in the
    app, and it exists only as an explicit second action — never on a
    timer, never on launch, never as a side effect of anything else.
    """
    batch = db.get_conn().execute(
        "SELECT id, purged_at FROM quarantine_batches WHERE id = ?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise QuarantineError(f"Batch {batch_id} not found")
    if batch["purged_at"]:
        raise QuarantineError(f"Batch {batch_id} was already deleted")

    batch_dir = os.path.join(str(quarantine_dir()), str(batch_id))
    deleted_bytes = db.get_conn().execute(
        "SELECT COALESCE(SUM(size), 0) AS n FROM quarantine_items WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()["n"]

    # ignore_errors=True was hiding every real failure here — a locked file,
    # a path over MAX_PATH, a permission refusal — while the rows below were
    # written anyway. That left the files on disk with the batch marked
    # purged, which recovery deliberately skips, so nothing in the app could
    # ever see or remove them again.
    failures = []

    def _on_error(func, path, exc_info):
        # The read-only attribute is the common Windows refusal. Clear it
        # and retry once; anything that still fails is a real failure.
        try:
            if IS_WINDOWS:
                os.chmod(path, S_IWRITE)
            func(path)
        except OSError as retry_err:
            # rmtree also reports the folders it couldn't remove because a
            # file inside survived. Those are consequences, not causes, so
            # only file-level failures are counted and quoted.
            if func in (os.unlink, os.remove):
                failures.append({"path": path, "error": str(retry_err)})
    shutil.rmtree(_long_path(batch_dir), onerror=_on_error)

    # The batch is only recorded as deleted when the folder is genuinely
    # gone. Anything left means the producer still has those files and must
    # be able to try again.
    if os.path.isdir(batch_dir):
        _drop_deleted_items(batch_id)
        raise QuarantineError(
            f"{max(len(failures), 1)} file(s) could not be deleted, so nothing was "
            f"marked as removed. First problem: "
            f"{failures[0]['error'] if failures else 'folder still present'}"
        )

    # The index rows go too, matching purge_item. Without this, every
    # batch-purged file left a row behind at state 'quarantined'
    # pointing at a file that no longer exists, and a later scan would
    # keep carrying files the producer deliberately destroyed.
    #
    # file_id 0 is a recovered item with no index row — nothing to
    # delete for those.
    file_ids = [
        (row["file_id"],)
        for row in db.get_conn().execute(
            "SELECT file_id FROM quarantine_items WHERE batch_id = ?",
            (batch_id,),
        ).fetchall()
        if row["file_id"]
    ]

    with db._write_lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM quarantine_items WHERE batch_id = ?", (batch_id,))
        if file_ids:
            conn.executemany("DELETE FROM files WHERE id = ?", file_ids)
        conn.execute(
            "UPDATE quarantine_batches SET purged_at = ? WHERE id = ?",
            (int(time.time()), batch_id),
        )
        conn.commit()

    return {"batch_id": batch_id, "purged": True, "bytes_freed": deleted_bytes}