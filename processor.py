# Core drumkit processing logic.
# Per staged file: re-validate (trust backstop), hash-dedupe, fingerprint
# genuinely new sounds, categorize (Tier 1 → Tier 2 → Tier 3), generate
# waveform peaks as a fingerprinting byproduct, and flag intra-user
# near-duplicates for review.
import os
import json
import hashlib
import subprocess
import tempfile
from typing import Optional
import librosa
import numpy as np
from r2_client import download_from_r2, upload_to_r2, delete_from_r2
from supabase_client import supabase
from notifier import notify_user, email_admin


# ── Constants — tune these as real usage data comes in ───────────────────────

MAX_DURATION_SECONDS = 90
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {".wav", ".aiff", ".aif", ".mp3", ".flac", ".ogg"}
SIMILARITY_THRESHOLD = 0.95  # near-dupe flag cutoff, cosine similarity on MFCC
WAVEFORM_NUM_POINTS = 1000


# ── Small helpers ─────────────────────────────────────────────────────────────

def compute_sha256(file_path: str) -> str:
    """Worker's own hash — this, not the client's claimed_hash, is what
    ever gets written to samples.hash. Client hash is a fast-path hint only."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_duration(file_path: str) -> Optional[float]:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", file_path],
            capture_output=True, text=True,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        if streams and streams[0].get("duration"):
            return float(streams[0]["duration"])
    except Exception as e:
        print(f"[processor] Duration probe failed: {e}")
    return None


def probe_codec(file_path: str) -> Optional[str]:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-select_streams", "a:0", "-show_streams", file_path],
            capture_output=True, text=True,
        )
        streams = json.loads(probe.stdout).get("streams", [])
        if streams:
            return streams[0].get("codec_name")
    except Exception as e:
        print(f"[processor] Codec probe failed: {e}")
    return None


def generate_waveform_peaks(y: np.ndarray, num_points: int = WAVEFORM_NUM_POINTS) -> list:
    """Same algorithm as the audio worker's generate_waveform_peaks — kept
    local since this is a separate repo, not importable across services."""
    chunk_size = max(1, len(y) // num_points)
    peaks = []
    for i in range(0, len(y) - chunk_size, chunk_size):
        chunk = y[i:i + chunk_size]
        peaks.append(float(np.max(np.abs(chunk))))
    max_peak = max(peaks) if peaks else 1.0
    return [round(p / max_peak, 4) for p in peaks]


def extract_features(y: np.ndarray, sr: int) -> dict:
    """
    Single audio load, multiple reuses: this same feature set backs the
    dedup fingerprint, the Tier 3 categorization fallback, and (via
    mfcc alone) the near-dupe cosine comparison — computed once here
    rather than three separate librosa passes.
    """
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)
    mfcc_mean = [float(x) for x in np.mean(mfcc, axis=1)]

    spectral_centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
    zcr = float(np.mean(librosa.feature.zero_crossing_rate(y)))
    duration = float(len(y) / sr)

    return {
        "mfcc": mfcc_mean,
        "spectral_centroid": spectral_centroid,
        "zcr": zcr,
        "duration": duration,
    }


def cosine_similarity(a: list, b: list) -> float:
    a_arr, b_arr = np.array(a), np.array(b)
    denom = np.linalg.norm(a_arr) * np.linalg.norm(b_arr)
    if denom == 0:
        return 0.0
    return float(np.dot(a_arr, b_arr) / denom)


# ── Category keyword lookup (Tier 1 / Tier 2) ─────────────────────────────────

def load_category_keywords() -> dict:
    """
    Pulls the backend-owned category_keywords table into a
    {category_name: [keyword, ...]} map. Fetched fresh per job rather
    than cached at module load — this table is meant to grow via the
    keyword_miss_log review flow, and a long-lived worker process
    shouldn't need a redeploy to pick up new approved keywords.
    """
    result = supabase.table("category_keywords").select("category_name, keyword").execute()
    keyword_map: dict = {}
    for row in result.data or []:
        keyword_map.setdefault(row["category_name"], []).append(row["keyword"].lower())
    return keyword_map


def match_category_by_keywords(text: str, keyword_map: dict) -> Optional[str]:
    text_lower = text.lower()
    for category, keywords in keyword_map.items():
        for kw in keywords:
            if kw in text_lower:
                return category
    return None


def classify_tier3(features: dict) -> Optional[str]:
    """
    Placeholder heuristic — no trained classifier exists yet. Returns
    None (unsure) in the general case; only handles the small number of
    cases where duration + spectral shape genuinely make one category
    dominant. Expand this once real labeled data comes in via the
    keyword_miss_log backfill loop rather than guessing more rules now.
    """
    duration = features["duration"]
    centroid = features["spectral_centroid"]
    zcr = features["zcr"]

    # Very short, very bright, high zero-crossing rate — hats/cymbals territory.
    if duration < 0.6 and centroid > 4000 and zcr > 0.15:
        return "Hats"

    # Short, low-centroid, low zero-crossing — kick/808 territory.
    if duration < 1.5 and centroid < 800 and zcr < 0.05:
        return "Kicks"

    return None  # stays unsure — Tier 3 didn't produce a confident guess


def categorize_file(relative_path: str, filename: str, features: dict, keyword_map: dict) -> tuple:
    """
    Returns (category, confidence, source). Runs the tiers in order,
    independent of whatever the client/backend already guessed —
    cheap to redo, and this is what actually gets written to samples.
    """
    folder_path = os.path.dirname(relative_path)

    tier1 = match_category_by_keywords(folder_path, keyword_map)
    if tier1:
        return tier1, "confident", "tier1_folder"

    tier2 = match_category_by_keywords(filename, keyword_map)
    if tier2:
        return tier2, "confident", "tier2_filename"

    tier3 = classify_tier3(features)
    if tier3:
        return tier3, "confident", "tier3_audio"

    return None, "unsure", "tier3_audio"


def log_keyword_miss(raw_token: str, resolved_category: str, sample_id: str) -> None:
    """
    Fires only when Tier 3 resolves something Tier 1/2 missed — the
    signal that feeds the keyword-list review queue described in the
    blueprint. Durable, no FKs, same pattern as tool_usage_log.
    """
    try:
        supabase.table("keyword_miss_log").insert({
            "raw_token": raw_token,
            "resolved_category": resolved_category,
            "sample_id": sample_id,
        }).execute()
    except Exception as e:
        print(f"[processor] keyword_miss_log insert failed: {e}")


# ── Folder resolution ──────────────────────────────────────────────────────────

def ensure_folder_id(drum_kit_id: str, user_id: str, relative_dir: str, folder_cache: dict) -> Optional[str]:
    """
    Walks a path like "808s/Distorted" one segment at a time, creating
    sample_folders rows as needed and reusing ones that already exist
    for this kit. folder_cache is keyed by the job's own dict so a
    multi-file kit only creates each folder once, not once per file.
    Returns None for root-level files (no folder).
    """
    if not relative_dir or relative_dir == ".":
        return None

    if relative_dir in folder_cache:
        return folder_cache[relative_dir]

    parts = relative_dir.split("/")
    parent_id = None
    accumulated = ""

    for part in parts:
        accumulated = f"{accumulated}/{part}" if accumulated else part

        if accumulated in folder_cache:
            parent_id = folder_cache[accumulated]
            continue

        existing = (
            supabase.table("sample_folders")
            .select("id")
            .eq("drum_kit_id", drum_kit_id)
            .eq("name", part)
            .eq("parent_folder_id", parent_id) if parent_id else
            supabase.table("sample_folders")
            .select("id")
            .eq("drum_kit_id", drum_kit_id)
            .eq("name", part)
            .is_("parent_folder_id", "null")
        ).execute()

        if existing.data:
            folder_id = existing.data[0]["id"]
        else:
            inserted = supabase.table("sample_folders").insert({
                "drum_kit_id": drum_kit_id,
                "parent_folder_id": parent_id,
                "user_id": user_id,
                "name": part,
            }).execute()
            folder_id = inserted.data[0]["id"]

        folder_cache[accumulated] = folder_id
        parent_id = folder_id

    return parent_id


# ── Job status helpers ─────────────────────────────────────────────────────────

def update_job_status(job_id: str, status: str, **extra) -> None:
    payload = {"status": status, **extra}
    supabase.table("drumkit_jobs").update(payload).eq("id", job_id).execute()


def _fail_job(job_id: str, error: str) -> None:
    update_job_status(job_id, "failed", error_message=error[:500])


def _notify_failure(user_id: str, job_id: str, stage: str, error: str) -> None:
    notify_user(
        user_id=user_id,
        notification_type="processing_failed",
        title="Drum kit processing failed",
        message="We couldn't process your drum kit. Please try uploading again.",
    )
    email_admin(
        subject=f"[Uforiya] Drumkit processing failed — {job_id}",
        html=f"""
        <div style="font-family: sans-serif; padding: 32px;">
            <h2 style="color: #c00;">Drumkit Processing Failed</h2>
            <p><strong>Stage:</strong> {stage}</p>
            <p><strong>Job ID:</strong> {job_id}</p>
            <p><strong>Error:</strong> {error}</p>
        </div>
        """,
    )


# ── Near-dupe comparison ────────────────────────────────────────────────────────

def get_user_sample_pool(user_id: str, category: Optional[str], exclude_sample_id: str) -> list:
    """
    Comparison pool for near-dupe detection — always the requesting
    user's own referenced samples, never anyone else's. Confident
    categories scope the pool for efficiency; unsure files compare
    against the user's full library, per the tiering design.
    """
    query = (
        supabase.table("user_samples")
        .select("sample_id, samples(id, fingerprint, category)")
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
    )
    result = query.execute()

    pool = []
    seen = set()
    for row in result.data or []:
        s = row.get("samples")
        if not s or not s.get("fingerprint") or s["id"] == exclude_sample_id or s["id"] in seen:
            continue
        if category and s.get("category") != category:
            continue
        seen.add(s["id"])
        pool.append((s["id"], s["fingerprint"]))
    return pool


def find_near_dupes(user_id: str, new_sample_id: str, fingerprint: dict, category: Optional[str], confidence: str) -> list:
    """Returns a list of (matched_sample_id, similarity_score) pairs at or above threshold."""
    scope_category = category if confidence == "confident" else None
    pool = get_user_sample_pool(user_id, scope_category, exclude_sample_id=new_sample_id)

    matches = []
    for other_id, other_fp in pool:
        score = cosine_similarity(fingerprint["mfcc"], other_fp["mfcc"])
        if score >= SIMILARITY_THRESHOLD:
            matches.append((other_id, round(score, 4)))
    return matches


# ── Main pipeline ────────────────────────────────────────────────────────────

def process_drumkit_job(job_id: str, user_id: str, drum_kit_id: str, files: list, worker_id: str) -> None:
    """
    Processes every staged file in a drumkit job.

    For each file:
      1. Download from staging
      2. Re-validate format / duration / size (trust backstop — client
         and backend already checked, this is the real gate)
      3. Re-hash — worker's own hash, not the client's claim, becomes
         samples.hash
      4. Hash lookup: existing → skip fingerprinting, just reference it.
         New → fingerprint, categorize, generate waveform, upload,
         insert samples row.
      5. Resolve/create the folder chain, insert user_samples row.
      6. For genuinely new samples, run intra-user near-dupe comparison
         and flag anything at or above threshold.

    Job ends in 'pending_review' if any flags were raised (blocks
    completion until the user resolves them), otherwise 'completed'.
    """
    update_job_status(job_id, "processing", worker_id=worker_id)

    keyword_map = load_category_keywords()
    folder_cache: dict = {}

    new_files_count = 0
    duplicate_files_count = 0
    flagged_pairs_count = 0
    new_sample_ids_this_job = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, staged in enumerate(files):
            staging_key = staged["staging_key"]
            relative_path = staged["relative_path"]
            filename = staged["filename"]

            local_path = os.path.join(tmp_dir, f"file_{i}{os.path.splitext(filename)[-1]}")

            print(f"[processor] Downloading staged file: {staging_key}")
            try:
                download_from_r2(staging_key, local_path)
            except Exception as e:
                print(f"[processor] Failed to download {staging_key}, skipping: {e}")
                continue

            # ── Re-validation (trust backstop) ────────────────────────────
            ext = os.path.splitext(filename)[-1].lower()
            if ext not in ALLOWED_EXTENSIONS:
                print(f"[processor] Rejected (bad format): {filename}")
                delete_from_r2(staging_key)
                continue

            duration = probe_duration(local_path)
            if duration is None or duration > MAX_DURATION_SECONDS:
                print(f"[processor] Rejected (duration): {filename} ({duration}s)")
                delete_from_r2(staging_key)
                continue

            if os.path.getsize(local_path) > MAX_FILE_SIZE_BYTES:
                print(f"[processor] Rejected (size): {filename}")
                delete_from_r2(staging_key)
                continue

            # ── Hash — worker's own, not the client's claim ───────────────
            real_hash = compute_sha256(local_path)

            existing = supabase.table("samples").select("id, category, fingerprint, category_confidence").eq("hash", real_hash).execute()

            if existing.data:
                # Already-owned-somewhere-on-the-platform sound. No new
                # fingerprinting needed — just create the reference.
                sample = existing.data[0]
                sample_id = sample["id"]
                duplicate_files_count += 1
                print(f"[processor] Hash match, referencing existing sample: {real_hash[:12]}")

                supabase.table("samples").update({
                    "reference_count": supabase.table("samples").select("reference_count").eq("id", sample_id).single().execute().data["reference_count"] + 1
                }).eq("id", sample_id).execute()
            else:
                # Genuinely new sound — full pipeline.
                print(f"[processor] New sample, fingerprinting: {filename}")
                try:
                    y, sr = librosa.load(local_path, sr=None, mono=True)
                except Exception as e:
                    print(f"[processor] Failed to load audio for {filename}, skipping: {e}")
                    delete_from_r2(staging_key)
                    continue

                features = extract_features(y, sr)
                category, confidence, source = categorize_file(relative_path, filename, features, keyword_map)

                # Backfill signal — Tier 3 catching what keywords missed.
                if source == "tier3_audio" and category:
                    token = os.path.basename(os.path.dirname(relative_path)) or filename
                    log_keyword_miss(token, category, None)  # sample_id filled in after insert below

                peaks = generate_waveform_peaks(y)
                waveform_path = os.path.join(tmp_dir, f"waveform_{i}.json")
                with open(waveform_path, "w") as f:
                    json.dump({"peaks": peaks, "length": len(peaks)}, f)

                permanent_key = f"samples/{real_hash}{ext}"
                waveform_key = f"samples/waveforms/{real_hash}.json"

                upload_to_r2(local_path, permanent_key, f"audio/{ext.lstrip('.')}")
                upload_to_r2(waveform_path, waveform_key, "application/json")

                inserted = supabase.table("samples").insert({
                    "hash": real_hash,
                    "r2_key": permanent_key,
                    "file_ext": ext.lstrip("."),
                    "duration_seconds": features["duration"],
                    "file_size_bytes": os.path.getsize(local_path),
                    "category": category,
                    "category_confidence": confidence,
                    "category_source": source,
                    "waveform_r2_key": waveform_key,
                    "fingerprint": features,
                    "reference_count": 1,
                }).execute()

                sample_id = inserted.data[0]["id"]
                new_files_count += 1
                new_sample_ids_this_job.append((sample_id, features, category, confidence))

                # ── Near-dupe comparison, intra-user only ──────────────────
                dupes = find_near_dupes(user_id, sample_id, features, category, confidence)
                for matched_id, score in dupes:
                    supabase.table("near_dupe_flags").insert({
                        "user_id": user_id,
                        "drumkit_job_id": job_id,
                        "sample_a_id": sample_id,
                        "sample_b_id": matched_id,
                        "similarity_score": score,
                        "status": "pending",
                    }).execute()
                    flagged_pairs_count += 1
                    print(f"[processor] Near-dupe flagged: {score * 100:.1f}% match")

            # ── Folder + user_samples row (runs for both new and existing) ──
            folder_dir = os.path.dirname(relative_path)
            folder_id = ensure_folder_id(drum_kit_id, user_id, folder_dir, folder_cache)

            supabase.table("user_samples").insert({
                "user_id": user_id,
                "sample_id": sample_id,
                "drum_kit_id": drum_kit_id,
                "folder_id": folder_id,
                "filename": filename,
            }).execute()

            delete_from_r2(staging_key)

        # ── Update kit's file_count ────────────────────────────────────────
        # INCREMENT, not overwrite — the backend already set an initial
        # file_count on drum_kits when it created the kit (covering
        # already-owned files it placed directly, with zero worker
        # involvement). Overwriting here would clobber that number
        # instead of adding this job's contribution on top of it.
        # Read-then-write, matching the same non-atomic pattern used
        # elsewhere in this file (e.g. reference_count) — fine at
        # current concurrency, worth a Postgres increment function
        # later if this becomes a hot path.
        total_processed_this_job = new_files_count + duplicate_files_count

        current_kit = supabase.table("drum_kits").select("file_count").eq("id", drum_kit_id).single().execute()
        current_count = current_kit.data.get("file_count", 0) if current_kit.data else 0

        supabase.table("drum_kits").update({
            "file_count": current_count + total_processed_this_job,
        }).eq("id", drum_kit_id).execute()

        # ── Finalize job ──────────────────────────────────────────────────
        if flagged_pairs_count > 0:
            update_job_status(
                job_id, "pending_review",
                total_files=total_processed_this_job,
                new_files=new_files_count,
                duplicate_files=duplicate_files_count,
                flagged_pairs_count=flagged_pairs_count,
                completed_at="now()",
            )
            notify_user(
                user_id=user_id,
                notification_type="drumkit_review_needed",
                title="Drum kit needs review",
                message=f"{flagged_pairs_count} possible duplicate{'s' if flagged_pairs_count != 1 else ''} found — review before continuing.",
                drum_kit_id=drum_kit_id,
            )
        else:
            update_job_status(
                job_id, "completed",
                total_files=total_processed_this_job,
                new_files=new_files_count,
                duplicate_files=duplicate_files_count,
                flagged_pairs_count=0,
                completed_at="now()",
            )
            notify_user(
                user_id=user_id,
                notification_type="processing_complete",
                title="Drum kit ready",
                message="Your drum kit has finished processing.",
                drum_kit_id=drum_kit_id,
            )

        print(f"[processor] Job {job_id} done: {new_files_count} new, {duplicate_files_count} deduped, {flagged_pairs_count} flagged")