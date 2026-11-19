"""A small MusicBrainz web-service client (https://musicbrainz.org/doc/MusicBrainz_API).

No API key is needed, but MusicBrainz asks every client to send a descriptive
User-Agent with contact details and to stay under one request per second. The
client keeps to that, retries when the server says it is busy, and caches JSON
responses on disk so repeated enrichment runs do not ask for the same data again.
"""
from library.services.http_cache import CachedJsonClient, ServiceError

BASE_URL = "https://musicbrainz.org/ws/2/"
PAGE_SIZE = 100


class MusicBrainzError(ServiceError):
    pass


class MusicBrainzClient(CachedJsonClient):
    base_url = BASE_URL
    service_name = "MusicBrainz"
    min_interval = 1.1  # MusicBrainz allows about one request per second
    cache_name = "musicbrainz_cache"
    error_class = MusicBrainzError

    def get(self, path, **params):
        return super().get(path, fmt="json", **params)

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
            inc="artist-rels+place-rels+labels+recordings+recording-level-rels+work-rels+work-level-rels+genres",
        )
