"""Runs the online lookups in a background thread so the web pages stay responsive.

Two kinds of job share one lock (both write to the same SQLite file): "musicbrainz"
(services.enrichment) and "notes" (services.insights: liner notes, lyrics, extraction).
"""
import threading
from dataclasses import dataclass, field

from django.db import connection
from django.utils import timezone

from .services import insights
from .services.enrichment import EnrichmentStats, enrich_library


@dataclass
class BackgroundJob:
    kind: str
    options: dict = field(default_factory=dict)
    running: bool = True
    started: object = field(default_factory=timezone.now)
    finished: object = None
    error: str = ""
    stop_requested: bool = False
    stats: object = None


RUNNERS = {
    "musicbrainz": (enrich_library, EnrichmentStats),
    "notes": (insights.run, insights.InsightStats),
}
_lock = threading.Lock()
_jobs = {}


def get_job(kind="musicbrainz"):
    return _jobs.get(kind)


def running_job():
    return next((job for job in _jobs.values() if job.running), None)


def _worker(job):
    runner, _ = RUNNERS[job.kind]
    try:
        def progress(stats):
            job.stats = stats
        job.stats = runner(progress=progress, should_stop=lambda: job.stop_requested, **job.options)
    except Exception as error:  # reported on the page instead of lost in a thread
        job.error = str(error)
    finally:
        job.running = False
        job.finished = timezone.now()
        _lock.release()
        connection.close()


def start_job(kind, **options):
    """Start a run; False when any job is already going."""
    if not _lock.acquire(blocking=False):
        return False
    job = BackgroundJob(kind=kind, options=options, stats=RUNNERS[kind][1]())
    _jobs[kind] = job
    threading.Thread(target=_worker, args=(job,), name=f"{kind}-lookup", daemon=True).start()
    return True


def start_enrichment(refresh=False, artist_ids=None):
    return start_job("musicbrainz", refresh=refresh, artist_ids=artist_ids)


def stop_job(kind):
    job = _jobs.get(kind)
    if job and job.running:
        job.stop_requested = True
        return True
    return False
