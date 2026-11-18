"""Runs MusicBrainz enrichment in a background thread so the web page stays responsive."""
import threading
from dataclasses import dataclass, field

from django.db import connection
from django.utils import timezone

from .services.enrichment import EnrichmentStats, enrich_library


@dataclass
class EnrichmentJob:
    refresh: bool = False
    artist_ids: list | None = None
    running: bool = True
    started: object = field(default_factory=timezone.now)
    finished: object = None
    error: str = ""
    stop_requested: bool = False
    stats: EnrichmentStats = field(default_factory=EnrichmentStats)


_lock = threading.Lock()
_job = None


def get_job():
    return _job


def _worker(job):
    try:
        def progress(stats):
            job.stats = stats
        job.stats = enrich_library(
            refresh=job.refresh,
            artist_ids=job.artist_ids,
            progress=progress,
            should_stop=lambda: job.stop_requested,
        )
    except Exception as error:  # reported on the page instead of lost in a thread
        job.error = str(error)
    finally:
        job.running = False
        job.finished = timezone.now()
        _lock.release()
        connection.close()


def start_enrichment(refresh=False, artist_ids=None):
    """Start a run; False when one is already going."""
    global _job
    if not _lock.acquire(blocking=False):
        return False
    _job = EnrichmentJob(refresh=refresh, artist_ids=artist_ids)
    threading.Thread(target=_worker, args=(_job,), name="musicbrainz-enrichment", daemon=True).start()
    return True


def stop_enrichment():
    if _job and _job.running:
        _job.stop_requested = True
        return True
    return False
