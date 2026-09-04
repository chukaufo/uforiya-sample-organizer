# FastAPI drumkit worker for Uforiya.
# Receives already-validated, already-hashed, staged files from the
# Next.js backend (client did parsing/validation/hashing, backend did
# the hash-check and staged only genuinely new files) and runs the
# expensive audio-analysis steps: re-validation, fingerprinting,
# categorization, near-dupe comparison, and waveform generation.
import os
import socket
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel
from dotenv import load_dotenv
from typing import List, Optional
from processor import process_drumkit_job

load_dotenv()

# Shared secret used to authenticate requests from the backend.
# Must match DRUMKIT_WORKER_SECRET in the Next.js backend env.
WORKER_SECRET = os.environ.get("DRUMKIT_WORKER_SECRET", "")

WORKER_ID = os.environ.get("RAILWAY_REPLICA_ID", socket.gethostname())

MAX_ATTEMPTS = 2

app = FastAPI(title="Uforiya Drumkit Worker")


# ─── Request Schema ───────────────────────────────────────────────────────────

class StagedFile(BaseModel):
    # R2 key where the client-uploaded file currently sits, e.g.
    # drumkits/staging/{job_id}/{index}.wav — never trust this alone,
    # the worker re-derives everything that matters from the actual bytes.
    staging_key: str
    # Client-computed SHA-256. Re-verified by the worker before it's
    # ever used as the permanent samples.hash value — client claims are
    # a fast path, not a trust boundary.
    claimed_hash: str
    # Full path within the kit, e.g. "808s/Distorted/spinz_808_dark.wav"
    relative_path: str
    filename: str
    # Category guess from client/backend Tier 1/2 keyword matching, if any.
    # Worker re-runs the same keyword check independently rather than
    # trusting this outright — cheap to redo, and closes the same trust
    # gap as everything else the client claims.
    claimed_category: Optional[str] = None


class ProcessDrumkitRequest(BaseModel):
    job_id: str
    user_id: str
    drum_kit_id: str
    files: List[StagedFile]


# ─── Auth Helper ─────────────────────────────────────────────────────────────

def verify_secret(request: Request) -> None:
    """Verify the Authorization header matches the shared worker secret."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ─── Background Task ──────────────────────────────────────────────────────────

def run_processing(job_id: str, user_id: str, drum_kit_id: str, files: list) -> None:
    """
    Background task that runs the full drumkit processing pipeline.
    Same retry-once-then-fail shape as the audio worker.
    """
    print(f"[worker] Starting drumkit job {job_id} ({len(files)} staged files)")

    try:
        process_drumkit_job(
            job_id=job_id,
            user_id=user_id,
            drum_kit_id=drum_kit_id,
            files=files,
            worker_id=WORKER_ID,
        )
    except Exception as e:
        print(f"[worker] Processing failed (attempt 1): {e}")
        try:
            print(f"[worker] Retrying job {job_id}...")
            process_drumkit_job(
                job_id=job_id,
                user_id=user_id,
                drum_kit_id=drum_kit_id,
                files=files,
                worker_id=WORKER_ID,
            )
        except Exception as retry_error:
            print(f"[worker] Retry also failed: {retry_error}")
            from processor import _fail_job, _notify_failure
            _fail_job(job_id, str(retry_error))
            _notify_failure(user_id, job_id, "final", str(retry_error))


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "uforiya-drumkit-worker"}


@app.post("/process")
async def process(
    body: ProcessDrumkitRequest,
    background_tasks: BackgroundTasks,
    request: Request,
):
    verify_secret(request)

    if not body.job_id or not body.user_id or not body.drum_kit_id:
        raise HTTPException(status_code=400, detail="job_id, user_id, and drum_kit_id are required")

    background_tasks.add_task(
        run_processing,
        job_id=body.job_id,
        user_id=body.user_id,
        drum_kit_id=body.drum_kit_id,
        files=[f.dict() for f in body.files],
    )

    return {"message": "Drumkit processing started", "job_id": body.job_id}