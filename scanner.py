# The walk and the index pass — milestone 1's substance.
#
# Two phases, and the split matters. The walk stats everything and
# writes rows; nothing is hashed during it. Grouping by size then
# decides which files are even candidates for hashing, and only those
# get read. In a real kit library that is a small fraction of the
# whole, which is the entire reason a 50,000-file scan finishes in
# minutes rather than hours.
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterator

import db
import hashing

# Directories never descended into.
#
# Cloud-sync caches are the important entries. Producers keep libraries
# in Dropbox and Google Drive folders, and on a placeholder-file setup
# touching a cache directory can trigger real downloads — a scan that
# silently pulls gigabytes over the network is the worst possible
# first-run experience.
SKIP_DIRS = {
    "$RECYCLE.BIN",
    "System Volume Information",
    "__MACOSX",
    "node_modules",
    ".Trash",
    ".Trashes",
    ".dropbox.cache",
}

# Hashing is I/O-bound, so threads rather than processes, and more
# threads than cores. Past ~16 an SSD is saturated and a spinning or
# USB disk is well past thrashing, so this is a ceiling and not a
# target.
HASH_THREADS = min(16, (os.cpu_count() or 4) * 2)

# How often the walk reports progress. Emitting per file would flood
# the SSE stream with tens of thousands of events for no benefit —
# nobody reads a path that flickers 2,000 times a second.
WALK_PROGRESS_INTERVAL = 200
HASH_PROGRESS_INTERVAL = 50


class Cancelled(Exception):
    """Raised inside a scan when the cancel flag is seen. Caught by the
    caller, which then reports the scan as cancelled rather than failed
    — a user-initiated stop is not an error."""


def _should_skip_dir(name: str) -> bool:
    # Leading-dot directories cover .git, .svn, .cache and the rest
    # without needing to enumerate them.
    return name in SKIP_DIRS or name.startswith(".")


def walk_audio_files(root_path: str) -> Iterator[tuple]:
    """
    Yields (rel_path, filename, size, mtime_ns) for every audio file
    beneath root_path.

    os.scandir rather than os.walk or pathlib.glob: scandir returns the
    directory entry's cached stat data, so size and mtime come free
    with the listing. pathlib re-stats every file and is several times
    slower across tens of thousands of entries — at this scale that is
    the difference between a walk measured in seconds and one measured
    in minutes.

    Symlinks and junctions are never followed. A Windows junction can
    point at its own ancestor, which makes the walk loop until it runs
    out of path length; a Mac symlink between two library folders would
    index the same file twice under different paths and then offer the
    producer a "duplicate" that is one file. follow_symlinks=False on
    the is_dir check is what prevents both.

    Iterative with an explicit stack rather than recursive, so a deeply
    nested library cannot exhaust Python's recursion limit.

    Directories that cannot be read are skipped silently. Permission
    errors are routine on Windows, and one unreadable folder must not
    end a scan of the other 49,000 files.
    """
    root_path = os.path.abspath(root_path)
    stack = [root_path]

    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if not _should_skip_dir(entry.name):
                        stack.append(entry.path)
                    continue

                if not entry.is_file(follow_symlinks=False):
                    continue

                if not hashing.is_audio_file(entry.name):
                    continue

                stat = entry.stat(follow_symlinks=False)

                # Zero-byte files are excluded from the index entirely,
                # not indexed and filtered later. Every empty file is
                # byte-identical to every other one, so indexing them
                # would produce a single enormous duplicate group that
                # is pure noise — and an empty file is not a sample
                # under any reading.
                if stat.st_size == 0:
                    continue

                rel = os.path.relpath(entry.path, root_path).replace("\\", "/")
                yield (rel, entry.name, stat.st_size, stat.st_mtime_ns)

            except OSError:
                # A file that vanished between the listing and the stat,
                # or one the process cannot open. Skip it.
                continue


def scan_root(
    root_id: int,
    root_path: str,
    emit: Callable[..., None],
    is_cancelled: Callable[[], bool],
) -> dict:
    """
    Walks a root, updates the index, and hashes what needs hashing.

    emit(phase, done, total, current_path) pushes a progress event.
    is_cancelled() is polled between files; when it returns True the
    scan raises Cancelled after flushing whatever it has written.

    Returns a summary dict for the job's completion event.
    """
    started = time.time()
    known = db.existing_files(root_id)

    seen_rel_paths = []
    unchanged = 0
    # rel_path -> (abs_path, size) for everything that needs hashing.
    needs_hash: dict = {}

    # ── Phase 1: walk ─────────────────────────────────────────────────
    #
    # Every file found is written, hashed or not. A row with size and
    # mtime but no sha256 is a valid intermediate state — it means the
    # file is known to exist and is a hash candidate. If the scan dies
    # here, the next one still skips re-statting what this one saw.
    with db.FileWriter(root_id) as writer:
        for rel, filename, size, mtime_ns in walk_audio_files(root_path):
            if is_cancelled():
                raise Cancelled()

            seen_rel_paths.append(rel)
            prior = known.get(rel)

            # The incremental check, and the whole reason a rescan
            # takes 15 seconds instead of 5 minutes. Same path, same
            # size, same mtime_ns — treat it as the same bytes and do
            # no I/O on it at all.
            #
            # mtime_ns rather than mtime: whole-second resolution would
            # miss a file edited twice within the same second, and
            # nanosecond precision costs nothing since scandir returns
            # it anyway.
            if (
                prior
                and prior["size"] == size
                and prior["mtime_ns"] == mtime_ns
                and prior["state"] == "present"
            ):
                unchanged += 1
                if len(seen_rel_paths) % WALK_PROGRESS_INTERVAL == 0:
                    emit("walking", len(seen_rel_paths), None, rel)
                continue

            abs_path = os.path.join(root_path, rel)
            needs_hash[rel] = (abs_path, size)
            writer.add(
                rel_path=rel,
                filename=filename,
                size=size,
                mtime_ns=mtime_ns,
                state="present",
            )

            if len(seen_rel_paths) % WALK_PROGRESS_INTERVAL == 0:
                emit("walking", len(seen_rel_paths), None, rel)

    emit("walking", len(seen_rel_paths), len(seen_rel_paths), None)

    # Files in the index that the walk did not find. Marked missing,
    # never deleted — an unplugged drive is indistinguishable from a
    # deleted folder here, and re-plugging should not cost a full
    # rehash of the whole drive.
    missing = db.mark_missing(root_id, seen_rel_paths)

    # ── Phase 2: tier 1, size bucketing ───────────────────────────────
    #
    # Free — every size came from the walk's stat data. A file whose
    # size is unique across the library cannot be a byte-for-byte
    # duplicate of anything, so it exits here having never been read.
    # This is what removes the large majority of a real library before
    # a single byte is hashed.
    #
    # Sizes are taken from the whole index rather than just this
    # root, so a file that is unique within its own folder but
    # duplicated on another drive is still caught.
    # Candidates come from the index, not from this walk. A file
    # indexed on an earlier scan that never earned a hash — because its
    # size was unique then — becomes a candidate the moment another
    # file of the same size appears anywhere in the library. Deriving
    # this from needs_hash instead would mean the older file is never
    # reconsidered and the pair is never found.
    candidates, skipped_unique_size = _pending_hash_work(root_id, root_path)

    # ── Phase 3: tiers 2 and 3, hashing ───────────────────────────────
    hashed = _hash_candidates(root_id, candidates, emit, is_cancelled)

    db.touch_root_scan(root_id)

    return {
        "root_id": root_id,
        "files_seen": len(seen_rel_paths),
        "unchanged": unchanged,
        "new_or_modified": len(needs_hash),
        "skipped_unique_size": skipped_unique_size,
        "hashed": hashed,
        "missing": missing,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def _sizes_with_duplicates() -> set:
    """
    Sizes appearing on more than one present file, across every root.

    One query instead of per-file lookups. Even a large library yields
    a modest set here — most sizes are unique, which is exactly why
    this tier is worth running.
    """
    rows = db.get_conn().execute(
        """
        SELECT size FROM files
        WHERE state = 'present'
        GROUP BY size
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    return {row["size"] for row in rows}


def _hash_candidates(
    root_id: int,
    candidates: dict,
    emit: Callable[..., None],
    is_cancelled: Callable[[], bool],
) -> int:
    """
    Partial-hashes every candidate that lacks one, then full-hashes
    only those whose partial hash is shared.

    Candidate values are (abs_path, size, stored_partial). A file
    carrying a stored partial from an earlier scan is not read again —
    its digest goes straight into the grouping. That is what keeps a
    file whose size was unique last time from being re-read on every
    rescan forever.

    Both passes run on a thread pool. The work is reading bytes, and
    Python releases the GIL during file reads, so threads give real
    parallelism — processes would add pickling overhead for nothing.

    Cancellation is checked as futures complete rather than before
    submission, so a cancel during hashing takes effect within one
    file rather than after the whole batch.
    """
    if not candidates:
        return 0

    total = len(candidates)
    partials: dict = {}
    computed_partials: dict = {}

    # Already known from a previous scan — no read needed.
    to_read = {}
    for rel, (abs_path, size, stored) in candidates.items():
        if stored:
            partials.setdefault(stored, []).append(rel)
        else:
            to_read[rel] = (abs_path, size)

    done = len(partials)

    if to_read:
        with ThreadPoolExecutor(max_workers=HASH_THREADS) as pool:
            futures = {
                pool.submit(hashing.compute_partial_hash, abs_path, size): rel
                for rel, (abs_path, size) in to_read.items()
            }
            for future in as_completed(futures):
                if is_cancelled():
                    # Stops the pool taking on more work; already-running
                    # reads finish, which is at most a few milliseconds.
                    pool.shutdown(wait=False, cancel_futures=True)
                    _store_partials(root_id, computed_partials)
                    raise Cancelled()

                rel = futures[future]
                result = future.result()
                if result is not None:
                    partials.setdefault(result, []).append(rel)
                    computed_partials[rel] = result

                done += 1
                if done % HASH_PROGRESS_INTERVAL == 0:
                    emit("hashing", done, total, rel)

    # Only newly computed partials need writing. Stored ones are
    # already in the row they came from.
    _store_partials(root_id, computed_partials)

    # Only partial hashes shared by two or more files justify a full
    # read. A unique partial hash means a unique file — and it keeps
    # its stored partial so the next scan skips it.
    full_candidates = [
        rel for group in partials.values() if len(group) > 1 for rel in group
    ]

    if not full_candidates:
        emit("hashing", total, total, None)
        return 0

    results: list = []

    with ThreadPoolExecutor(max_workers=HASH_THREADS) as pool:
        futures = {
            pool.submit(hashing.compute_sha256, candidates[rel][0]): rel
            for rel in full_candidates
        }
        for future in as_completed(futures):
            if is_cancelled():
                pool.shutdown(wait=False, cancel_futures=True)
                _store_sha256(root_id, results)
                raise Cancelled()

            rel = futures[future]
            digest = future.result()
            if digest is not None:
                results.append((digest, rel))

            done += 1
            if done % HASH_PROGRESS_INTERVAL == 0:
                emit("hashing", total, total, rel)

    _store_sha256(root_id, results)
    emit("hashing", total, total, None)
    return len(results)
def _pending_hash_work(root_id: int, root_path: str) -> tuple:
    """
    Files under this root still missing a full hash, split into those
    whose size is shared (candidates) and those whose size is unique
    (skipped).

    Reading this from the index rather than from the walk is what makes
    hashing decisions cumulative. Each scan reconsiders every
    unhashed file against the whole library's current set of sizes, so
    a duplicate that only becomes detectable later is still caught.

    The stored partial_hash rides along so a file that was partial-
    hashed on a previous scan is not read again.
    """
    rows = db.get_conn().execute(
        """
        SELECT rel_path, size, partial_hash
        FROM files
        WHERE root_id = ? AND state = 'present' AND sha256 IS NULL
        """,
        (root_id,),
    ).fetchall()

    shared_sizes = _sizes_with_duplicates()

    candidates = {}
    skipped = 0
    for row in rows:
        if row["size"] in shared_sizes:
            candidates[row["rel_path"]] = (
                os.path.join(root_path, row["rel_path"]),
                row["size"],
                row["partial_hash"],
            )
        else:
            skipped += 1
    return candidates, skipped


def _store_partials(root_id: int, computed: dict) -> None:
    """Writes newly computed partial hashes back in one statement."""
    if not computed:
        return
    rows = [(digest, root_id, rel) for rel, digest in computed.items()]
    with db._write_lock:
        conn = db.get_conn()
        conn.executemany(
            "UPDATE files SET partial_hash = ? WHERE root_id = ? AND rel_path = ?",
            rows,
        )
        conn.commit()


def _store_sha256(root_id: int, results: list) -> None:
    """Writes full hashes back in one statement. Called on the
    cancellation path too — a hash already computed is worth keeping."""
    if not results:
        return
    rows = [(digest, root_id, rel) for digest, rel in results]
    with db._write_lock:
        conn = db.get_conn()
        conn.executemany(
            "UPDATE files SET sha256 = ? WHERE root_id = ? AND rel_path = ?",
            rows,
        )
        conn.commit()
