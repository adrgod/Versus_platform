"""Front covers from the Cover Art Archive (https://coverartarchive.org), the artwork partner of MusicBrainz.

Free, no key. It is asked by MusicBrainz release-group ID, which the online lookup already stores.
"""
import urllib.error

from library.services.http_cache import CachedJsonClient, ServiceError
from library.services.images import download_image


class CoverArtError(ServiceError):
    pass


class CoverArtClient(CachedJsonClient):
    base_url = "https://coverartarchive.org/"
    service_name = "Cover Art Archive"
    min_interval = 0.5
    error_class = CoverArtError

    def front_cover(self, release_group_mbid, size=500):
        """JPEG/PNG bytes of the front cover, or None when the archive has none."""
        self._wait_turn()
        url = f"{self.base_url}release-group/{release_group_mbid}/front-{size}"
        try:
            return download_image(url, self.user_agent)
        except (urllib.error.URLError, TimeoutError) as error:
            raise CoverArtError(f"Could not fetch the cover from the Cover Art Archive: {error}") from error
