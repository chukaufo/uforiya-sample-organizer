# Quarantine: the only code in this app that moves a producer's files.
#
# Nothing is ever deleted here. Approved files are moved to a staging
# folder in app-data, the index records where each came from, and a
# JSON manifest is written beside them. Permanent deletion is a
# separate, later action the user takes deliberately.
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
from typing import Optional

import db
import hashing
from platform_paths import quarantine_dir

MANIFEST_NAME = "manifest.json"


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

def _move_file(source: str, destination: str) -> None:
    """
    Moves one file, verifying the copy when a rename is not possible.

    os.rename is atomic within a volume and is what runs in the normal
    case — the file is never in two places or neither. Across volumes
    it raises, and the fallback is copy, verify by hash, then delete
    the source. Deleting before verifying is the one way this app
    could actually lose a sample, so the verification is not optional.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)

    try:
        os.rename(source, destination)
        return
    except OSError:
        pass  # Cross-volume, or a filesystem that refuses the rename.

    shutil.copy2(source, destination)

    if not hashing.verify_copy(source, destination):
        # Leave the source untouched and clean up the bad copy.
        try:
            os.remove(destination)
        except OSError:
            pass
        raise QuarantineError(
            f"Refused: copy of {os.path.basename(source)} did not verify. "
            "Original left in place."
        )

    os.remove(source)


def _write_manifest(batch_dir: str, batch_id: int, items: list) -> None:
    """
    Writes the batch's contents as JSON inside the quarantine folder
    itself, as well as to SQLite.

    If the index is ever lost or corrupted, the files are still
    restorable by hand from this file. Redundancy is cheap here and the
    failure it covers is unrecoverable otherwise.
    """
    manifest = {
        "batch_id": batch_id,
        "created_at": int(time.time()),
        "items": [
            {
                "original_path": item["original_path"],
                "quarantine_path": item["quarantine_path"],
                "sha256": item["sha256"],
                "size": item["size"],
            }
            for item in items
        ],
    }
    with open(os.path.join(batch_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


# ── Public API ───────────────────────────────────────────────────────────────

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
        abs_path = os.path.join(row["root_path"], row["rel_path"])
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

    moved = []
    failures = []

    for row, abs_path in prepared:
        # Relative path preserved inside the batch, so two files named
        # Clap.wav from different kits cannot collide, and a restore by
        # hand is legible.
        destination = os.path.join(batch_dir, row["rel_path"])
        try:
            _move_file(abs_path, destination)
        except (QuarantineError, OSError) as exc:
            failures.append({"path": row["rel_path"], "error": str(exc)})
            continue

        moved.append({
            "file_id": row["id"],
            "original_path": abs_path,
            "quarantine_path": destination,
            "sha256": row["sha256"],
            "size": row["size"],
        })

    if moved:
        _write_manifest(batch_dir, batch_id, moved)

    total_bytes = sum(item["size"] for item in moved)

    with db._write_lock:
        conn = db.get_conn()
        conn.executemany(
            "INSERT INTO quarantine_items "
            "(batch_id, file_id, original_path, quarantine_path, sha256, size) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (batch_id, m["file_id"], m["original_path"],
                 m["quarantine_path"], m["sha256"], m["size"])
                for m in moved
            ],
        )
        conn.executemany(
            "UPDATE files SET state = 'quarantined' WHERE id = ?",
            [(m["file_id"],) for m in moved],
        )
        conn.execute(
            "UPDATE quarantine_batches SET file_count = ?, bytes = ? WHERE id = ?",
            (len(moved), total_bytes, batch_id),
        )
        conn.commit()

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

    restored = []
    failures = []

    for item in items:
        if os.path.exists(item["original_path"]):
            failures.append({
                "path": item["original_path"],
                "error": "A file already exists at this path; left untouched.",
            })
            continue
        try:
            _move_file(item["quarantine_path"], item["original_path"])
        except (QuarantineError, OSError) as exc:
            failures.append({"path": item["original_path"], "error": str(exc)})
            continue
        restored.append(item)

    if restored:
        with db._write_lock:
            conn = db.get_conn()
            conn.executemany(
                "DELETE FROM quarantine_items WHERE id = ?",
                [(item["id"],) for item in restored],
            )
            # Rows go back to 'present' by file id. Their hashes are
            # still valid — the bytes never changed, only the location.
            conn.executemany(
                "UPDATE files SET state = 'present' WHERE id = ?",
                [(item["file_id"],) for item in restored],
            )
            conn.execute(
                "UPDATE quarantine_batches SET file_count = file_count - ?, "
                "bytes = bytes - ? WHERE id = ?",
                (
                    len(restored),
                    sum(item["size"] for item in restored),
                    batch_id,
                ),
            )
            conn.commit()

    return {
        "batch_id": batch_id,
        "restored": len(restored),
        "failures": failures,
    }

    

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

    shutil.rmtree(batch_dir, ignore_errors=True)

    with db._write_lock:
        conn = db.get_conn()
        conn.execute("DELETE FROM quarantine_items WHERE batch_id = ?", (batch_id,))
        conn.execute(
            "UPDATE quarantine_batches SET purged_at = ? WHERE id = ?",
            (int(time.time()), batch_id),
        )
        conn.commit()

    return {"batch_id": batch_id, "purged": True, "bytes_freed": deleted_bytes}