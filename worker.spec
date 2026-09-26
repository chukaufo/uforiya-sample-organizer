# PyInstaller spec for the worker.
#
# Frozen as --onedir, not --onefile: onefile unpacks the whole bundle to a
# temp directory on every launch, which adds seconds to startup and is the
# single most common trigger for antivirus false positives on Windows.
#
# Must be built on the target OS — PyInstaller cannot cross-compile, so the
# Windows binary comes from the Windows machine.
import os
import shutil
import sys

# FFmpeg ships beside the worker so preview playback works for formats
# Chromium cannot decode — AIFF above all, which is native on Mac and
# which no browser has ever supported.
#
# Found on the build machine's PATH rather than committed to the repo: it
# is ~80MB, it is platform-specific, and a binary in git is a binary
# nobody updates. The build fails loudly if it is missing, because the
# alternative is shipping an app whose preview button silently fails on
# half a producer's library.
_FFMPEG_NAME = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
_ffmpeg = shutil.which(_FFMPEG_NAME) or shutil.which('ffmpeg')

if not _ffmpeg:
    raise SystemExit(
        f'FFmpeg not found on PATH. Install it and rebuild:\n'
        f'  macOS:   brew install ffmpeg\n'
        f'  Windows: winget install Gyan.FFmpeg  (then reopen the terminal)'
    )

a = Analysis(
    ['main.py'],
    pathex=[],
    # ('.',) puts it next to worker.exe, which is exactly where
    # platform_paths.bundled_binary looks when frozen.
    binaries=[(_ffmpeg, '.')],
    datas=[('category_keywords.json', '.')],
    # uvicorn loads its protocol and lifespan implementations by string name
    # at runtime, so static analysis never sees them and they are missing
    # from the bundle unless named here. The failure looks like the worker
    # starting and then dying with no traceback.
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'uvicorn.lifespan.off',
    ],
    hookspath=[],
    runtime_hooks=[],
    # Nothing in the worker draws anything. Excluding these keeps a GUI
    # toolkit and a plotting stack out of a bundle that has no use for them.
    excludes=['tkinter', 'matplotlib', 'PIL', 'pytest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='worker',
    debug=False,
    strip=False,
    upx=False,          # UPX compression is another antivirus trigger
    console=True,       # windowsHide in main.js keeps the window from showing
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='worker',
)
