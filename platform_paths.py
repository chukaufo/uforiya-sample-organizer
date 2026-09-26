# Every OS-specific path decision lives here and nowhere else.
#
# Development happens on macOS, shipping target is Windows. The bugs
# that class of split produces — separator handling, app-data location,
# where a frozen binary's siblings live — are all path bugs, and they
# surface late if `if sys.platform` checks are scattered through the
# codebase. Keeping them in one module means Windows breakage shows up
# in one file.
#
# NOT named platform.py on purpose: that shadows the stdlib module of
# the same name, and any `import platform` elsewhere in the worker
# would silently resolve to this file instead.
import os
import sys
from pathlib import Path
from typing import Optional

APP_NAME = "Uforiya"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle rather than source."""
    return getattr(sys, "frozen", False)


def app_data_dir() -> Path:
    """
    Per-user application data directory, created if absent.

    Deliberately never inside the user's sample library. A producer who
    moves or renames their library folder must not lose the index, and
    an index file sitting among their samples is one more thing for
    them to accidentally delete.
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif IS_MACOS:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"

    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    """The SQLite index. One file, one user, one machine."""
    return app_data_dir() / "library.db"


def quarantine_dir() -> Path:
    """
    Root of the quarantine staging area. Each approved removal batch
    gets its own subdirectory beneath this, named by batch id.

    Created eagerly so its absence can never be the reason a
    quarantine move fails halfway through a batch.
    """
    path = app_data_dir() / "quarantine"
    path.mkdir(parents=True, exist_ok=True)
    return path


def restored_dir(near: Optional[str] = None) -> Path:
    """
    Where a restored file goes when its original folder is gone.

    A producer who renamed or deleted a kit folder made that decision on
    purpose. Recreating the folder to put a file back fights it, and
    leaves them with both the folder they renamed and the one they
    didn't. Everything that has lost its home lands here instead, in one
    place they can deal with deliberately.

    Beside the library root rather than inside it, so a rescan does not
    immediately re-index the restored copies as duplicates of whatever
    is still in the library.

    `near` is the library root the file came from. When that is gone
    too — the root was removed, or the drive is not connected — app-data
    is the fallback, because it is the one location guaranteed to exist
    and be writable.
    """
    if near:
        parent = Path(near).parent
        if parent.is_dir():
            path = parent / f"{APP_NAME} Restored"
            try:
                path.mkdir(parents=True, exist_ok=True)
                return path
            except OSError:
                pass   # Read-only, or gone between the check and the call.

    path = app_data_dir() / "Restored"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_binary(name: str) -> str:
    """
    Resolves a binary that ships alongside the worker (currently just
    FFmpeg, needed from milestone 3).

    Frozen: PyInstaller --onedir puts data files next to the executable
    and sets sys._MEIPASS to that directory. Both are checked because
    the two have differed across PyInstaller major versions and the
    failure mode — a FileNotFoundError deep inside a scan — is not
    worth the ambiguity.

    Development: falls through to the bare name, letting PATH resolve
    it. Homebrew's ffmpeg on the Mac, whatever is installed on the CI
    runner.

    Returns a string rather than a Path because it is passed straight
    to subprocess, and Windows subprocess handling of Path objects is
    inconsistent enough to not be worth relying on.
    """
    exe = f"{name}.exe" if IS_WINDOWS else name

    if is_frozen():
        candidates = [Path(sys.executable).parent / exe]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / exe)
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

    return exe


def normalize_path(path: str) -> str:
    """
    Canonical form for anything stored in the index.

    Forward slashes everywhere, including Windows. Windows accepts them
    in every API call, and the alternative is an index whose rows are
    not portable and whose prefix-rewriting (needed when a kit folder
    moves) has to branch per platform.

    Case is left alone. Windows filesystems are case-insensitive but
    case-preserving, and lowercasing would mean showing a producer a
    path that does not match what Explorer displays. The cost is that a
    path differing only in case could theoretically index twice on
    Windows — accepted, since the walk generates these paths itself
    rather than taking them from user input.
    """
    return str(Path(path).resolve()).replace("\\", "/")
