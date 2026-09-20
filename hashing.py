# Content hashing: the two tiers that read bytes off disk.
#
# Tier 1 (size bucketing) needs no reads at all and lives in the
# scanner. Tier 4 (decoded PCM) needs FFmpeg and arrives in milestone 3.
# This module covers tiers 2 and 3 — partial and full hashing.
#
# Both are I/O-bound, not CPU-bound. SHA-256 on a modern CPU runs at
# hundreds of MB/s; a SATA SSD does maybe 500MB/s and a USB drive far
# less. That is why these run on a thread pool rather than a process
# pool: threads release the GIL during file reads, and the pickling
# cost of shipping file contents between processes would dominate.
import hashlib
import os
from typing import Optional

# Audio formats worth indexing. Drum kits ship as WAV overwhelmingly,
# with AIFF from Mac-native producers and MP3/OGG in older or
# bandwidth-conscious packs.
#
# MIDI and preset files are deliberately excluded. They are small,
# rarely duplicated in a way that costs disk, and a producer who finds
# their .fst presets listed as duplicates alongside samples would
# reasonably wonder what else this app thinks it owns.
AUDIO_EXTENSIONS = {".wav", ".aiff", ".aif", ".mp3", ".flac", ".ogg"}

# Bytes read from each end for the tier-2 partial hash.
#
# 64KB from the head catches format headers and the attack transient,
# which is where two different one-shots diverge fastest. 64KB from the
# tail catches the decay, which is where two files sharing a common
# source library but differing in trim or fade diverge instead.
#
# Head-only would be wrong: WAV files carry a fixed-layout header, and
# two different 44.1kHz stereo one-shots of identical length share
# their first 44 bytes exactly.
PARTIAL_CHUNK_BYTES = 64 * 1024

# Read size for full hashing. 1MB balances syscall overhead against
# memory: a 200MB loop pack read whole would spike RSS badly on a
# producer's machine while doing nothing for throughput.
READ_CHUNK_BYTES = 1024 * 1024


def is_audio_file(filename: str) -> bool:
    """Extension check only — no magic-byte sniffing.

    The walk hits tens of thousands of entries and opening each one to
    read its header would make the cheapest phase of the scan the most
    expensive. A mislabelled file gets caught later, when tier 4 tries
    to decode it and FFmpeg refuses.
    """
    return os.path.splitext(filename)[1].lower() in AUDIO_EXTENSIONS


def compute_partial_hash(path: str, size: int) -> Optional[str]:
    """
    Tier 2: hash of the first and last PARTIAL_CHUNK_BYTES, plus the
    size itself.

    Only runs on files whose size is shared with at least one other
    file, so it is never the thing that touches the whole library.
    Roughly 0.1ms per file — two seeks and two small reads.

    The size is mixed into the digest so that a file smaller than one
    chunk (hashed whole here) can never collide with the partial hash
    of a larger file whose head and tail happen to match.

    Returns None on any read error. An unreadable file is not a fatal
    condition: permission-denied and files-in-use are both normal on
    Windows, and one bad file must not abort a 50,000-file scan.
    """
    h = hashlib.sha256()
    h.update(str(size).encode())
    try:
        with open(path, "rb") as f:
            if size <= PARTIAL_CHUNK_BYTES * 2:
                # Small enough that reading both chunks would overlap
                # or exceed the file. Hash it whole — it is cheap, and
                # it makes the result exact rather than approximate.
                h.update(f.read())
            else:
                h.update(f.read(PARTIAL_CHUNK_BYTES))
                f.seek(-PARTIAL_CHUNK_BYTES, os.SEEK_END)
                h.update(f.read(PARTIAL_CHUNK_BYTES))
    except OSError:
        return None
    return h.hexdigest()


def compute_sha256(path: str) -> Optional[str]:
    """
    Tier 3: full content hash. The digest that decides byte-identical
    duplication, and the one stored as files.sha256.

    Runs only on files that survived tiers 1 and 2, which in a real kit
    library is a small fraction of the whole.

    Returns None on read error, same reasoning as compute_partial_hash.
    """
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(READ_CHUNK_BYTES), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def verify_copy(source: str, destination: str) -> bool:
    """
    Confirms two files are byte-identical by hashing both.

    Used by quarantine (milestone 4) on the cross-volume path, where a
    move degrades to copy-then-delete. Deleting a source before
    verifying its copy is the one way this app could actually lose a
    producer's sample, so the check is not optional there.

    Lives here rather than in the quarantine module because it is
    hashing, and because a single place to change the digest algorithm
    is worth more than locality.
    """
    a = compute_sha256(source)
    if a is None:
        return False
    b = compute_sha256(destination)
    return b is not None and a == b
