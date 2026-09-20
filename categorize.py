# Filename and folder-based sample categorization.
#
# NOT USED IN V1. Nothing imports this module. It is here because the
# matcher below is the one genuinely valuable thing carried over from
# the cloud drumkit worker — four rounds of bug fixes are encoded in
# it, and rewriting it later would mean rediscovering the same bugs.
#
# Its eventual purpose is auto-sort: filing whole kit folders into
# type folders, not flattening a library into kicks/ and snares/.
# Drums stay in their original kits; a producer looking for "that
# snare from the Cole Bennett kit" finds it by kit.
#
# Keyword matching only, no audio analysis. Producers name files
# consistently enough that keywords get most of the way, and it is
# instant. Audio-based classification needs a trained model and is a
# much larger project.
#
# The tier-3 audio heuristic from the cloud worker is deliberately not
# carried over: it depended on librosa spectral features, and this app
# has no librosa and no need for one.
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from platform_paths import bundled_binary  # noqa: F401  (frozen-path parity)

KEYWORDS_FILENAME = "category_keywords.json"

_keyword_map: Optional[dict] = None


def load_category_keywords(path: Optional[str] = None) -> dict:
    """
    Loads {category: [keyword, ...]} from the bundled JSON file.

    Cached after the first read. In the cloud worker this was fetched
    per job from a Postgres table so a keyword could be added without
    a redeploy; locally there is no server to fetch from and adding a
    keyword means shipping a new build either way.
    """
    global _keyword_map
    if _keyword_map is not None and path is None:
        return _keyword_map

    target = Path(path) if path else Path(__file__).parent / KEYWORDS_FILENAME
    with open(target, "r", encoding="utf-8") as f:
        raw = json.load(f)

    loaded = {
        category: [kw.lower() for kw in keywords]
        for category, keywords in raw.items()
    }
    if path is None:
        _keyword_map = loaded
    return loaded


def _tokenize(text: str) -> list:
    """
    Extracts alphanumeric runs, splitting on everything else (spaces,
    underscores, hyphens, brackets, parens, periods).

    Digits must stay part of their run rather than act as a delimiter.
    An earlier version split on [_\\-\\s\\d]+, treating digit runs
    themselves as separators — "808 [100]" tokenized to ['[', ']'],
    "808s" tokenized to ['s']. Any purely-numeric keyword like "808"
    became permanently unmatchable, so matching silently failed on
    every 808 folder and filename.
    """
    return re.findall(r'[a-z0-9]+', text.lower())


def _fuzzy_match(token: str, keyword: str) -> bool:
    """
    Edit-distance similarity via difflib (stdlib, no dependency).

    The threshold scales with keyword length: short keywords (3-4
    letters, "hat", "808") need a tighter ratio than long ones, since
    a one-character difference on a short word is a much larger
    relative change. This is what catches abbreviations and typos like
    "kck" for "kick" that exact matching never will.
    """
    if token == keyword:
        return True

    threshold = 0.85 if len(keyword) <= 4 else 0.75
    return SequenceMatcher(None, token, keyword).ratio() >= threshold


def match_category_by_keywords(text: str, keyword_map: dict) -> Optional[str]:
    """
    Returns the best-matching category, not the first one found.

    Three passes, strongest signal first:
      1. raw substring — catches keywords embedded in longer names
         ("My 808 Collection", "808bass"). Longest keyword wins, so
         "808" does not beat a more specific match.
      2. exact token match
      3. fuzzy token match

    An earlier version returned the first category whose keyword
    matched anything, and dict ordering came from however the database
    returned rows — so a file matching both "kick" and "808" got
    whichever loaded first, non-deterministically.
    """
    lowered = text.lower()
    tokens = _tokenize(text)

    best_category = None
    best_length = 0
    for category, keywords in keyword_map.items():
        for kw in keywords:
            if kw in lowered and len(kw) > best_length:
                best_category = category
                best_length = len(kw)
    if best_category:
        return best_category

    if not tokens:
        return None

    for category, keywords in keyword_map.items():
        for token in tokens:
            if token in keywords:
                return category

    for category, keywords in keyword_map.items():
        for token in tokens:
            for kw in keywords:
                if _fuzzy_match(token, kw):
                    return category
    return None


def match_folder_chain(rel_path: str, keyword_map: dict) -> Optional[str]:
    """
    Resolves a category from the folder chain, nearest ancestor first.

    Matching the whole path at once is wrong: "Open Hats & Crashes/Crash"
    contains both "open hat" (8 chars) and "crash" (5), and the
    longest-keyword rule would hand every file in Crash/ to Open Hats.
    The immediate parent is the most specific statement about what a
    file is, so it decides; only if it says nothing do we ask its
    parent.
    """
    folder_path = os.path.dirname(rel_path)
    if not folder_path or folder_path == ".":
        return None

    for part in reversed([p for p in folder_path.split("/") if p]):
        match = match_category_by_keywords(part, keyword_map)
        if match:
            return match
    return None


def categorize(rel_path: str, filename: str, keyword_map: Optional[dict] = None) -> tuple:
    """
    Returns (category, source).

    Folder first, then filename. A file that matches neither returns
    (None, None) rather than a catch-all bucket — auto-sort should
    leave a kit it cannot classify exactly where it is, not move it
    somewhere arbitrary.
    """
    keyword_map = keyword_map or load_category_keywords()

    folder_match = match_folder_chain(rel_path, keyword_map)
    if folder_match:
        return folder_match, "folder"

    filename_match = match_category_by_keywords(filename, keyword_map)
    if filename_match:
        return filename_match, "filename"

    return None, None
