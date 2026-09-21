# SQLite index: schema, connection handling, and batched writes.
#
# Everything the worker knows about a library lives here. The index is
# authoritative for what has been seen; the filesystem is authoritative
# for what exists. Reconciling the two is scanner.py's job, not this
# module's — db.py only stores and retrieves.
import sqlite3
import threading
import time
from typing import Iterable, Optional

from platform_paths import db_path

# Bumped when the schema changes in a way that needs migration code.
# Stored in SQLite's own user_version pragma rather than a table, so
# reading it costs nothing and cannot itself fail on an old database.
SCHEMA_VERSION = 1

# Rows accumulated before a flush. 1000 keeps each transaction short
# enough that a cancelled scan loses little work, while still being far
# past the point where per-row commit overhead dominates — that
# overhead is usually the single biggest cost in a scanner like this.
WRITE_BATCH_SIZE = 1000

_conn: Optional[sqlite3.Connection] = None
# SQLite serialises writes internally, but Python's sqlite3 module can
# still interleave a transaction across threads in ways that produce
# confusing errors. Hashing runs on a thread pool, so every write goes
# through this lock and the transaction boundaries stay meaningful.
_write_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS roots (
  id           INTEGER PRIMARY KEY,
  path         TEXT UNIQUE NOT NULL,
  added_at     INTEGER NOT NULL,
  last_scan_at INTEGER
);

CREATE TABLE IF NOT EXISTS files (
  id           INTEGER PRIMARY KEY,
  root_id      INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
  rel_path     TEXT NOT NULL,
  filename     TEXT NOT NULL,
  size         INTEGER NOT NULL,
  mtime_ns     INTEGER NOT NULL,
  partial_hash TEXT,
  sha256       TEXT,
  pcm_hash     TEXT,
  duration_ms  INTEGER,
  peaks        BLOB,
  state        TEXT NOT NULL DEFAULT 'present',
  indexed_at   INTEGER NOT NULL,
  UNIQUE(root_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_files_size  ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_sha   ON files(sha256);
CREATE INDEX IF NOT EXISTS idx_files_pcm   ON files(pcm_hash);
CREATE INDEX IF NOT EXISTS idx_files_state ON files(state);
CREATE INDEX IF NOT EXISTS idx_files_root  ON files(root_id);

CREATE TABLE IF NOT EXISTS quarantine_batches (
  id          INTEGER PRIMARY KEY,
  created_at  INTEGER NOT NULL,
  file_count  INTEGER NOT NULL,
  bytes       INTEGER NOT NULL,
  purged_at   INTEGER
);

CREATE TABLE IF NOT EXISTS quarantine_items (
  id              INTEGER PRIMARY KEY,
  batch_id        INTEGER NOT NULL REFERENCES quarantine_batches(id),
  file_id         INTEGER NOT NULL,
  original_path   TEXT NOT NULL,
  quarantine_path TEXT NOT NULL,
  sha256          TEXT,
  size            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quarantine_items_batch
  ON quarantine_items(batch_id);
"""


def get_conn() -> sqlite3.Connection:
    """
    The one connection, opened lazily.

    check_same_thread=False because FastAPI serves requests on a thread
    pool and scans run on their own threads — a connection pinned to
    one thread would be unusable. Safety comes from _write_lock instead.

    WAL is what lets a read (serving /duplicates) proceed while a scan
    is writing. Without it the whole UI blocks for the duration of a
    scan, which is exactly the experience this app exists to avoid.

    synchronous=NORMAL trades an fsync per commit for durability only
    against OS crash, not process crash. The index is fully rebuildable
    from disk, so the trade is free.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(db_path()), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        # Keeps the temp B-trees used by the GROUP BY in the duplicates
        # query off disk. A 50k-file grouping is small enough to stay
        # in memory comfortably.
        _conn.execute("PRAGMA temp_store=MEMORY")
        _init_schema(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current == 0:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def close() -> None:
    """Called on shutdown. Leaving WAL files behind is harmless but
    closing cleanly means the next launch does no recovery pass."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ── Roots ────────────────────────────────────────────────────────────────────

def add_root(path: str) -> int:
    """
    Registers a library folder. Returns the root id, existing or new.

    Idempotent on purpose: adding a folder that is already indexed is a
    rescan request, not an error, and the UI should not have to know
    the difference.
    """
    now = int(time.time())
    with _write_lock:
        conn = get_conn()
        existing = conn.execute(
            "SELECT id FROM roots WHERE path = ?", (path,)
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO roots (path, added_at) VALUES (?, ?)", (path, now)
        )
        conn.commit()
        return cur.lastrowid


def list_roots() -> list:
    rows = get_conn().execute(
        """
        SELECT r.id, r.path, r.added_at, r.last_scan_at,
               COUNT(f.id) FILTER (WHERE f.state = 'present') AS file_count
        FROM roots r
        LEFT JOIN files f ON f.root_id = r.id
        GROUP BY r.id
        ORDER BY r.added_at
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_root(root_id: int) -> Optional[dict]:
    row = get_conn().execute(
        "SELECT id, path, added_at, last_scan_at FROM roots WHERE id = ?",
        (root_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_root(root_id: int) -> bool:
    """Forgets a root and every file row under it. Touches no actual
    files on disk — this is 'stop tracking this folder', never a delete."""
    with _write_lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM roots WHERE id = ?", (root_id,))
        conn.commit()
        return cur.rowcount > 0


def touch_root_scan(root_id: int) -> None:
    with _write_lock:
        conn = get_conn()
        conn.execute(
            "UPDATE roots SET last_scan_at = ? WHERE id = ?",
            (int(time.time()), root_id),
        )
        conn.commit()


# ── Files ────────────────────────────────────────────────────────────────────

def existing_files(root_id: int) -> dict:
    """
    Every indexed file under a root, keyed by relative path.

    This is the whole basis of incremental rescan: the walk compares
    each file it finds against this map, and anything whose size and
    mtime_ns are unchanged is skipped without hashing or decoding.

    Loading it all at once rather than querying per file is deliberate.
    50k rows of this shape is a few megabytes, and 50k individual
    SELECTs would cost more than the walk itself.
    """
    rows = get_conn().execute(
        """
        SELECT id, rel_path, size, mtime_ns, partial_hash, sha256, state
        FROM files WHERE root_id = ?
        """,
        (root_id,),
    ).fetchall()
    return {row["rel_path"]: dict(row) for row in rows}


class FileWriter:
    """
    Accumulates file rows and flushes them in batches inside a
    transaction. Used as a context manager so a cancelled or failed
    scan still commits what it managed to index rather than losing it.

    The upsert preserves hashes on conflict only when the file is
    unchanged. A file whose size or mtime moved is a different file as
    far as the index is concerned, so its stale hashes are cleared and
    it will be rehashed on the next pass.
    """

    UPSERT = """
        INSERT INTO files
          (root_id, rel_path, filename, size, mtime_ns,
           partial_hash, sha256, pcm_hash, duration_ms, peaks,
           state, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(root_id, rel_path) DO UPDATE SET
          size         = excluded.size,
          mtime_ns     = excluded.mtime_ns,
          partial_hash = excluded.partial_hash,
          sha256       = excluded.sha256,
          pcm_hash     = excluded.pcm_hash,
          duration_ms  = excluded.duration_ms,
          peaks        = excluded.peaks,
          state        = excluded.state,
          indexed_at   = excluded.indexed_at
    """

    def __init__(self, root_id: int, batch_size: int = WRITE_BATCH_SIZE):
        self.root_id = root_id
        self.batch_size = batch_size
        self._pending: list = []
        self.written = 0

    def add(
        self,
        rel_path: str,
        filename: str,
        size: int,
        mtime_ns: int,
        partial_hash: Optional[str] = None,
        sha256: Optional[str] = None,
        pcm_hash: Optional[str] = None,
        duration_ms: Optional[int] = None,
        peaks: Optional[bytes] = None,
        state: str = "present",
    ) -> None:
        self._pending.append((
            self.root_id, rel_path, filename, size, mtime_ns,
            partial_hash, sha256, pcm_hash, duration_ms, peaks,
            state, int(time.time()),
        ))
        if len(self._pending) >= self.batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        with _write_lock:
            conn = get_conn()
            conn.executemany(self.UPSERT, self._pending)
            conn.commit()
        self.written += len(self._pending)
        self._pending.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # Flushes even on exception. A scan that dies partway has still
        # done real work and the next rescan should not repeat it.
        self.flush()
        return False


def mark_missing(root_id: int, present_rel_paths: Iterable[str]) -> int:
    """
    Flags rows whose files were not seen in this walk as 'missing'
    rather than deleting them.

    An unplugged external drive looks identical to a deleted folder
    from the walk's point of view. Deleting the rows would mean a full
    rehash of the entire drive the next time it is plugged in, which on
    USB is the slowest thing this app can do. 'missing' costs a row and
    makes reconnection free.

    Rows already quarantined are left alone — their files are genuinely
    not at the original path, by design.
    """
    present = set(present_rel_paths)
    with _write_lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT rel_path FROM files WHERE root_id = ? AND state = 'present'",
            (root_id,),
        ).fetchall()
        gone = [(r["rel_path"],) for r in rows if r["rel_path"] not in present]
        if gone:
            conn.executemany(
                f"UPDATE files SET state = 'missing' "
                f"WHERE root_id = {int(root_id)} AND rel_path = ?",
                gone,
            )
            conn.commit()
        return len(gone)


def counts(root_id: Optional[int] = None) -> dict:
    """Row counts by state, for /health and the scan summary."""
    if root_id is None:
        rows = get_conn().execute(
            "SELECT state, COUNT(*) AS n FROM files GROUP BY state"
        ).fetchall()
    else:
        rows = get_conn().execute(
            "SELECT state, COUNT(*) AS n FROM files WHERE root_id = ? GROUP BY state",
            (root_id,),
        ).fetchall()
    return {row["state"]: row["n"] for row in rows}
