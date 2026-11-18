"""A polite JSON-over-HTTP client: one request per interval, retries when busy, answers cached on disk.

Shared by the MusicBrainz and LRCLIB clients.
"""
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from library import repository

DEFAULT_USER_AGENT = "VersusLibrary/0.1 ( personal music archive )"
MAX_ATTEMPTS = 5


class ServiceError(Exception):
    pass


class CachedJsonClient:
    """Subclasses set ``base_url``, ``service_name``, ``min_interval`` and ``cache_name``."""

    base_url = ""
    service_name = "the service"
    min_interval = 1.0
    cache_name = "http_cache"
    error_class = ServiceError

    _lock = threading.Lock()
    _last_request = {}

    def __init__(self, *, user_agent=None, cache_dir=None, cache_days=None, use_cache=True):
        settings = repository.get_config().get("enrichment", {})
        self.user_agent = user_agent or settings.get("user_agent") or DEFAULT_USER_AGENT
        self.cache_dir = Path(cache_dir) if cache_dir else repository.PROJECT_ROOT / "db" / self.cache_name
        self.cache_seconds = 86400 * (cache_days if cache_days is not None else settings.get("cache_days", 30))
        self.use_cache = use_cache
        self.request_count = 0

    def get(self, path, **params):
        query = urllib.parse.urlencode(sorted(params.items()))
        url = f"{self.base_url}{path}?{query}"

        cache_file = self.cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()}.json"
        if self.use_cache and cache_file.exists() and time.time() - cache_file.stat().st_mtime < self.cache_seconds:
            return json.loads(cache_file.read_text(encoding="utf-8"))

        data = self._fetch(url)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(data), encoding="utf-8")
        return data

    def _fetch(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._wait_turn()
            self.request_count += 1
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                if error.code == 404:
                    return None
                if error.code in (429, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                raise self.error_class(f"{self.service_name} answered {error.code} for {url}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                raise self.error_class(f"Could not reach {self.service_name}: {error}") from error
        raise self.error_class(f"{self.service_name} stayed busy for {url}")

    def _wait_turn(self):
        key = type(self).__name__
        with CachedJsonClient._lock:
            delay = CachedJsonClient._last_request.get(key, 0.0) + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            CachedJsonClient._last_request[key] = time.monotonic()
