# Duplicate grouping — reading the index, not scanning.
#
# Groups are a query, never a table. Materialising them would mean
# invalidating on every scan for no gain: the grouping is a GROUP BY
# over an indexed column, and SQLite answers it in milliseconds even
# on a large library.
#
# Only exact content matches are duplicates. Two files are in a group
# if their full SHA-256 is identical, or (from milestone 3) their
# decoded PCM hash is. No similarity scoring, no thresholds — a
# producer deciding whether to delete a sample needs certainty, and a
# "97% similar" verdict is the one thing that got this wrong before.
import os
from typing import Optional

import db
from platform_paths import normalize_path

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def _kit_name(rel_path: str) -> str:
    """
    Best guess at which kit a file belongs to: the folder two levels
    up, or one if that is the root.

    A heuristic and not a truth — kits nest inconsistently, and a
    "deconstructed" kit may be five levels deep. It is here because
    "which kit is this in" is the question a producer is actually
    asking, and a full path is slow to read in a list of eleven rows.
    The full path is always shown alongside it, so a wrong guess
    misleads nobody.
    """
    parts = [p for p in os.path.dirname(rel_path).split("/") if p]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return parts[-2]


def list_groups(
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    match_type: Optional[str] = None,
) -> dict:
    """
    Duplicate groups, ordered by reclaimable bytes descending.

    Biggest wins first is the right default: a producer clearing space
    wants the 40MB loop duplicated six times before the 200KB hat
    duplicated twice, and scrolling to find it would be absurd.

    Exact (sha256) and re-encoded (pcm_hash) groups are unioned. A
    file can appear in both — identical to one file, re-encoded from
    another — which is correct rather than a bug, but the UI must
    treat the two groups as separate decisions.

    Paged. Never return 410 groups at once, let alone the thousands a
    large library produces.
    """
    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    offset = (page - 1) * page_size

    # A single UNION ALL over the two hash columns, so paging and
    # ordering apply across both rather than per-type. The literal
    # match_type rides along so the caller knows which column produced
    # each group without a second query.
    base = """
        SELECT 'exact'      AS match_type, sha256   AS hash_value,
               COUNT(*)     AS copies,     MIN(size) AS size
        FROM files
        WHERE state = 'present' AND sha256 IS NOT NULL
        GROUP BY sha256 HAVING COUNT(*) > 1

        UNION ALL

        SELECT 're-encoded' AS match_type, pcm_hash AS hash_value,
               COUNT(*)     AS copies,     MIN(size) AS size
        FROM files
        WHERE state = 'present' AND pcm_hash IS NOT NULL
        GROUP BY pcm_hash HAVING COUNT(*) > 1
    """

    if match_type in ("exact", "re-encoded"):
        rows = db.get_conn().execute(
            f"""
            SELECT * FROM ({base})
            WHERE match_type = ?
            ORDER BY size * (copies - 1) DESC, copies DESC
            LIMIT ? OFFSET ?
            """,
            (match_type, page_size, offset),
        ).fetchall()
        total = db.get_conn().execute(
            f"SELECT COUNT(*) AS n FROM ({base}) WHERE match_type = ?",
            (match_type,),
        ).fetchone()["n"]
    else:
        rows = db.get_conn().execute(
            f"""
            SELECT * FROM ({base})
            ORDER BY size * (copies - 1) DESC, copies DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()
        total = db.get_conn().execute(
            f"SELECT COUNT(*) AS n FROM ({base})"
        ).fetchone()["n"]

    groups = []
    for row in rows:
        groups.append({
            "group_id": f"{'sha256' if row['match_type'] == 'exact' else 'pcm'}:{row['hash_value']}",
            "match_type": row["match_type"],
            "copies": row["copies"],
            # One copy always survives, so the reclaimable figure is
            # never the full size of the group.
            "reclaimable_bytes": row["size"] * (row["copies"] - 1),
        })

    return {
        "groups": groups,
        "page": page,
        "page_size": page_size,
        "total_groups": total,
        "has_more": offset + len(groups) < total,
    }


def totals() -> dict:
    """Headline numbers for the review screen — how much is actually
    recoverable across the whole library."""
    row = db.get_conn().execute(
        """
        SELECT COUNT(*) AS groups,
               COALESCE(SUM(size * (copies - 1)), 0) AS reclaimable_bytes,
               COALESCE(SUM(copies - 1), 0)          AS removable_files
        FROM (
            SELECT COUNT(*) AS copies, MIN(size) AS size
            FROM files
            WHERE state = 'present' AND sha256 IS NOT NULL
            GROUP BY sha256 HAVING COUNT(*) > 1
        )
        """
    ).fetchone()
    return dict(row)


def _parse_group_id(group_id: str) -> tuple:
    """
    Splits 'sha256:9f2a...' or 'pcm:4c1e...' into (column, value).

    Prefixed rather than bare so a group id is self-describing and an
    exact group can never be confused with a re-encoded one whose
    digest happens to collide in the UI's state.
    """
    if ":" not in group_id:
        raise ValueError("Malformed group_id")

    prefix, value = group_id.split(":", 1)
    column = {"sha256": "sha256", "pcm": "pcm_hash"}.get(prefix)
    if column is None or not value:
        raise ValueError("Malformed group_id")
    return column, value


def get_group(group_id: str) -> Optional[dict]:
    """
    One group with every member: full path, size, duration, kit guess,
    waveform peaks, and a keep suggestion.

    Returns None if the group no longer has two members — a scan since
    the list was fetched may have removed one, and offering a
    "duplicate" with one copy left is worse than an empty result.
    """
    column, value = _parse_group_id(group_id)

    rows = db.get_conn().execute(
        f"""
        SELECT f.id, f.rel_path, f.filename, f.size, f.mtime_ns,
               f.duration_ms, f.peaks, r.path AS root_path
        FROM files f
        JOIN roots r ON r.id = f.root_id
        WHERE f.state = 'present' AND f.{column} = ?
        ORDER BY f.rel_path
        """,
        (value,),
    ).fetchall()

    if len(rows) < 2:
        return None

    files = []
    for row in rows:
        files.append({
            "file_id": row["id"],
            "path": normalize_path(os.path.join(row["root_path"], row["rel_path"])),
            "filename": row["filename"],
            "kit": _kit_name(row["rel_path"]),
            "size": row["size"],
            "mtime_ns": row["mtime_ns"],
            "duration_ms": row["duration_ms"],
            # Peaks arrive in milestone 3. Null here rather than absent
            # so the renderer's shape does not change when they land.
            "peaks": None,
            "suggested_keep": False,
        })

    _apply_keep_suggestion(files)

    size = rows[0]["size"]
    return {
        "group_id": group_id,
        "match_type": "exact" if column == "sha256" else "re-encoded",
        "copies": len(files),
        "reclaimable_bytes": size * (len(files) - 1),
        "files": files,
    }


def _apply_keep_suggestion(files: list) -> None:
    """
    Marks one file as the suggested keep: shallowest path, ties broken
    by oldest mtime.

    Shallowest usually means the original kit rather than a nested
    re-download, and oldest usually means the copy that was there
    first. Both are guesses, which is why this is a proposal and not a
    decision — every checkbox stays user-controlled, and "select all"
    operates on the proposal rather than on everything.
    """
    if not files:
        return

    def rank(f: dict) -> tuple:
        depth = f["path"].count("/")
        return (depth, f["mtime_ns"] or 0, f["path"])

    keeper = min(files, key=rank)
    keeper["suggested_keep"] = True
