"""Work out the country of a band's "country" tag that is really a city, and keep the city.

``locate("Budapest")`` -> ``("Hungary", "Budapest", "city")``; ``locate("Oslo, Norway")`` -> ``("Norway", "Oslo", "")``;
``locate("Hungary")`` -> ``("Hungary", "", "")``; an unknown text -> ``("", text, "")``.

Order of work: a known country name (flags.py), then "City, Country" / "City (Country)" pieces,
then the bundled list of cities with 15,000+ people (data/cities.tsv.gz, GeoNames, CC-BY 4.0),
then the answers kept in the PlaceCountry table, and last an online lookup (Nominatim) whose
answer, found or not, is kept in that table so no place is searched twice.
"""
import gzip
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from library import repository
from library.services.flags import CODE_NAMES, NAMES, flag_code
from library.services.http_cache import CachedJsonClient, ServiceError

CITY_FILE = Path(__file__).resolve().parent.parent / "data" / "cities.tsv.gz"
_SPLIT = re.compile(r"\s*[,;/()\[\]]+\s*|\s+-\s+")


def fold(text):
    """Lower case without accents: "Göteborg " -> "goteborg"."""
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).strip()


@lru_cache(maxsize=1)
def _cities():
    table = {}
    if CITY_FILE.exists():
        with gzip.open(CITY_FILE, "rt", encoding="utf-8") as handle:
            for line in handle:
                name, code, _ = line.rstrip("\n").split("\t")
                table[name] = code
    return table


def country_name(code):
    return CODE_NAMES.get(code, "")


def is_country(text):
    return flag_code(text) is not None or (text or "").strip().casefold() in NAMES


class NominatimClient(CachedJsonClient):
    base_url = "https://nominatim.openstreetmap.org/"
    service_name = "OpenStreetMap Nominatim"
    min_interval = 1.1  # their usage policy: at most one request a second
    cache_name = "nominatim_cache"

    def _fetch(self, url):  # one quick try: a missing network must not stall an import
        import json, urllib.error, urllib.request
        self._wait_turn()
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": self.accept})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise ServiceError(f"Could not reach {self.service_name}: {error}") from error

    def country_of(self, place):
        rows = self.get("search", q=place, format="json", addressdetails=1, limit=1, **{"accept-language": "en"}) or []
        return ((rows[0].get("address") or {}).get("country", "") if rows else "")


_unreachable = []


def _online_enabled():
    return bool(repository.get_config().get("enrichment", {}).get("place_lookup_online", True))


def _canonical(country):
    code = flag_code(country)
    return CODE_NAMES.get(code, country) if code else country


def city_country(place, *, online=True):
    """Country of a city name, or "" when nobody knows it. Never raises."""
    key = fold(place)
    if len(key) < 3:
        return ""
    code = _cities().get(key)
    if code:
        return country_name(code)
    from library.models import PlaceCountry  # late: the models import services
    kept = PlaceCountry.objects.filter(place_key=key).first()
    if kept is not None:
        return kept.country
    if not (online and _online_enabled()):
        return ""
    if _unreachable:
        return ""
    try:
        country = _canonical(NominatimClient().country_of(place))
    except (ServiceError, ValueError, OSError):
        _unreachable.append(True)  # offline: stop trying until the server restarts
        return ""  # a failed call is not an answer: ask again next time
    PlaceCountry.objects.update_or_create(place_key=key, defaults={"place": place[:150], "country": country[:100]})
    return country


def locate(text, *, online=True, hint_country=""):
    """(country, place, via) for a "country" tag; via is "" (read from the text), "hint" or "city".
    ``hint_country`` is a country another source gave (MusicBrainz, Metal Archives): used instead of
    searching for the city."""
    raw = (text or "").strip()
    if not raw:
        return "", "", ""
    if is_country(raw):
        return raw, "", ""
    pieces = [piece for piece in _SPLIT.split(raw) if piece]
    for position in range(len(pieces) - 1, -1, -1):  # "City, Country": the country is usually last
        if is_country(pieces[position]):
            rest = [piece for index, piece in enumerate(pieces) if index != position]
            return pieces[position], ", ".join(rest), ""
    if hint_country:
        return hint_country, raw, "hint"
    for piece in pieces:
        country = city_country(piece, online=online)
        if country:
            return country, raw, "city"
    return "", raw, ""
