"""A small MusicBrainz web-service client (https://musicbrainz.org/doc/MusicBrainz_API).

No API key is needed, but MusicBrainz asks every client to send a descriptive
User-Agent with contact details and to stay under one request per second. The
client keeps to that, retries when the server says it is busy, and caches JSON
responses on disk so repeated enrichment runs do not ask for the same data again.
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

BASE_URL = "https://musicbrainz.org/ws/2/"
DEFAULT_USER_AGENT = "VersusLibrary/0.1 ( personal music archive )"
MIN_INTERVAL = 1.1  # seconds between requests
MAX_ATTEMPTS = 5
PAGE_SIZE = 100


class MusicBrainzError(Exception):
    pass


def _settings():
    return repository.get_config().get("enrichment", {})


def cache_directory():
    return repository.PROJECT_ROOT / "db" / "musicbrainz_cache"


class MusicBrainzClient:
    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, *, user_agent=None, cache_dir=None, cache_days=None, use_cache=True):
        settings = _settings()
        self.user_agent = user_agent or settings.get("user_agent") or DEFAULT_USER_AGENT
        self.cache_dir = Path(cache_dir) if cache_dir else cache_directory()
        self.cache_seconds = 86400 * (cache_days if cache_days is not None else settings.get("cache_days", 30))
        self.use_cache = use_cache
        self.request_count = 0

    # -- low level -----------------------------------------------------------
    def get(self, path, **params):
        params["fmt"] = "json"
        query = urllib.parse.urlencode(sorted(params.items()))
        url = f"{BASE_URL}{path}?{query}"

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
                raise MusicBrainzError(f"MusicBrainz answered {error.code} for {url}") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                raise MusicBrainzError(f"Could not reach MusicBrainz: {error}") from error
        raise MusicBrainzError(f"MusicBrainz stayed busy for {url}")

    @classmethod
    def _wait_turn(cls):
        with cls._lock:
            delay = cls._last_request + MIN_INTERVAL - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            cls._last_request = time.monotonic()

    # -- endpoints -----------------------------------------------------------
    def search_artists(self, name, limit=10):
        escaped = name.replace("\\", "\\\\").replace('"', '\\"')
        data = self.get("artist", query=f'artist:"{escaped}" OR alias:"{escaped}"', limit=limit) or {}
        return data.get("artists", [])

    def artist(self, mbid):
        return self.get(f"artist/{mbid}", inc="aliases+genres+tags+ratings+artist-rels")

    def release_groups(self, artist_mbid, max_items=500):
        """Every release group credited to the artist (albums, EPs, singles, live...)."""
        items, offset = [], 0
        while offset < max_items:
            data = self.get(
                "release-group", artist=artist_mbid, inc="ratings+genres",
                limit=PAGE_SIZE, offset=offset,
            ) or {}
            page = data.get("release-groups", [])
            items.extend(page)
            offset += len(page)
            if not page or offset >= data.get("release-group-count", 0):
                break
        return items

    def release_group(self, mbid):
        return self.get(f"release-group/{mbid}", inc="artist-credits+ratings+genres")

    def releases_of_group(self, release_group_mbid):
        data = self.get("release", **{"release-group": release_group_mbid}, status="official", limit=PAGE_SIZE) or {}
        return data.get("releases", [])

    def release(self, mbid):
        return self.get(
            f"release/{mbid}",
            inc="artist-rels+place-rels+labels+recordings+recording-level-rels+genres",
        )
