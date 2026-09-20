# Job registry and progress streaming.
#
# A scan takes minutes on a first run. No HTTP request blocks for that,
# so long work returns a job_id immediately and the caller subscribes
# to an SSE stream for progress. This module owns the job lifecycle;
# scanner.py owns the work, and main.py owns the routes.
#
# Everything here lives in memory. Jobs do not survive a worker
# restart, which is correct — a scan interrupted by a crash should be
# restarted by the user, not silently resumed, and the index already
# holds whatever the dead scan managed to write.
import queue
import threading
import time
import uuid
from typing import Optional

# Jobs kept after finishing. The renderer may reconnect to an SSE
# stream a moment after a scan ends and needs to see the terminal
# event rather than a 404. Beyond a handful this is just memory held
# for nothing.
COMPLETED_JOB_LIMIT = 10

# Seconds between SSE keepalives when no progress event has fired.
# Without these, a long tier-3 hash of one enormous file looks to the
# renderer like a dead connection, and some proxies and runtimes drop
# idle streams outright.
KEEPALIVE_INTERVAL = 15

PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL_STATES = {COMPLETED, FAILED, CANCELLED}


class Job:
    """
    One unit of long-running work, with a progress queue and a cancel
    flag.

    The queue is unbounded. Progress events are emitted at intervals
    (every 200 files walked, every 50 hashed) rather than per file, so
    a renderer that is slow to drain cannot make this grow without
    limit in practice — and a bounded queue that blocks the scan
    thread when full would be strictly worse than memory pressure.
    """

    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.state = PENDING
        self.created_at = time.time()
        self.finished_at: Optional[float] = None
        self.result: Optional[dict] = None
        self.error: Optional[str] = None

        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._cancel = threading.Event()
        # Last progress event, replayed to a subscriber that connects
        # mid-scan so its progress bar starts populated rather than at
        # zero.
        self.last_progress: Optional[dict] = None

    # ── Cancellation ─────────────────────────────────────────────────

    def cancel(self) -> None:
        """
        Requests a stop. Cooperative — the scan checks this flag
        between files and raises, rather than being killed.

        Forcible termination is not an option worth having: the scan
        holds a SQLite transaction and an open file handle, and killing
        it mid-write is how an index gets corrupted.
        """
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    # ── Progress ─────────────────────────────────────────────────────

    def emit(
        self,
        phase: str,
        done: Optional[int] = None,
        total: Optional[int] = None,
        current_path: Optional[str] = None,
    ) -> None:
        """
        Pushes a progress event to every subscriber.

        eta_seconds is computed from elapsed time and completion ratio
        — crude, and deliberately so. A more sophisticated estimate
        would still be wrong on the case that matters (an external USB
        drive where throughput varies by an order of magnitude), and
        the honest answer to "how long" is a moving number the producer
        watches rather than a promise.
        """
        event = {
            "phase": phase,
            "done": done,
            "total": total,
            "current_path": current_path,
            "eta_seconds": self._estimate_eta(done, total),
        }
        self.last_progress = event
        self._queue.put(event)

    def _estimate_eta(self, done: Optional[int], total: Optional[int]) -> Optional[int]:
        if not done or not total or done <= 0:
            return None
        elapsed = time.time() - self.created_at
        rate = done / elapsed
        if rate <= 0:
            return None
        return int((total - done) / rate)

    def finish(self, state: str, result: Optional[dict] = None, error: Optional[str] = None) -> None:
        """Marks the job terminal and closes every stream watching it."""
        self.state = state
        self.finished_at = time.time()
        self.result = result
        self.error = error
        self._queue.put({
            "phase": state,
            "result": result,
            "error": error,
        })
        # Sentinel — tells subscribers the stream is over, distinct
        # from an event that merely reports a terminal state.
        self._queue.put(None)

    def events(self):
        """
        Generator yielding progress events until the job finishes.

        Yields the last known progress first so a renderer connecting
        mid-scan sees the current state immediately rather than waiting
        for the next interval.

        None yielded means keepalive — main.py turns it into an SSE
        comment line, which keeps the connection warm without being a
        message the renderer has to handle.
        """
        if self.last_progress:
            yield self.last_progress

        while True:
            try:
                event = self._queue.get(timeout=KEEPALIVE_INTERVAL)
            except queue.Empty:
                if self.state in TERMINAL_STATES:
                    return
                yield None
                continue

            if event is None:
                return
            yield event

    def summary(self) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class JobRegistry:
    """
    Tracks jobs and enforces one scan at a time.

    The single-scan rule is not a simplification — two concurrent
    walks of the same disk contend for I/O and finish slower than
    either would alone, and the UI has one progress bar. A second scan
    request while one is running returns the in-flight job rather than
    refusing, so the renderer can simply subscribe and show progress.
    """

    def __init__(self):
        self._jobs: dict = {}
        self._lock = threading.Lock()
        self._active_scan: Optional[str] = None

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def active_scan(self) -> Optional[Job]:
        with self._lock:
            if self._active_scan is None:
                return None
            job = self._jobs.get(self._active_scan)
            if job is None or job.state in TERMINAL_STATES:
                self._active_scan = None
                return None
            return job

    def start_scan(self, target, kind: str = "scan") -> tuple:
        """
        Starts a scan on its own thread, or returns the running one.

        Returns (job, started) — started is False when an existing scan
        was returned instead, so the route can answer 200 with the
        in-flight job_id rather than pretending it launched something.

        target is called as target(job) and must accept cancellation
        via job.is_cancelled().
        """
        existing = self.active_scan()
        if existing is not None:
            return existing, False

        job = Job(kind)
        with self._lock:
            self._jobs[job.id] = job
            self._active_scan = job.id

        thread = threading.Thread(
            target=self._run,
            args=(job, target),
            name=f"scan-{job.id[:8]}",
            daemon=True,
        )
        thread.start()
        return job, True

    def _run(self, job: Job, target) -> None:
        """
        Runs the job body and records how it ended.

        Cancelled is imported locally to keep this module free of a
        scanner import — jobs.py knows nothing about walking files, and
        a top-level import would make the dependency circular the
        moment scanner needs anything from here.
        """
        from scanner import Cancelled

        job.state = RUNNING
        try:
            result = target(job)
            job.finish(COMPLETED, result=result)
        except Cancelled:
            job.finish(CANCELLED)
        except Exception as exc:
            # The message reaches the UI, so it has to be something a
            # producer can act on. Full tracebacks go to stdout, which
            # Electron captures for a log file.
            import traceback
            traceback.print_exc()
            job.finish(FAILED, error=f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                if self._active_scan == job.id:
                    self._active_scan = None
            self._prune()

    def _prune(self) -> None:
        """Drops the oldest finished jobs past the retention limit."""
        with self._lock:
            finished = [
                j for j in self._jobs.values()
                if j.state in TERMINAL_STATES and j.finished_at
            ]
            if len(finished) <= COMPLETED_JOB_LIMIT:
                return
            finished.sort(key=lambda j: j.finished_at)
            for job in finished[: len(finished) - COMPLETED_JOB_LIMIT]:
                self._jobs.pop(job.id, None)


registry = JobRegistry()
