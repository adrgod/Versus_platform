"""Faceted browsing over albums: pick values in any group, see who is in the selection.

Every group maps its values to sets of album ids. Artist-level values (country,
members, ...) apply to all of that artist's albums. Within a group the chosen
values are OR-ed, across groups they are AND-ed, and each value's count is
computed against the other groups' choices, so counts always say how many
albums choosing that value would show.
"""
import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from library.models import Album, AlbumFacet, AlbumProfile, Artist, ArtistFacet, ArtistProfile, FacetKind


@dataclass
class Group:
    key: str
    label: str
    level: str  # "artist" or "album": what the value describes
    ordered: bool = False  # values sort by name (decades, ratings) instead of by count
    values: dict = field(default_factory=dict)  # value key -> {"name", "albums": set}

    def add(self, name, album_ids):
        name = (name or "").strip()
        if not name:
            return
        entry = self.values.setdefault(name.casefold(), {"name": name, "albums": set()})
        entry["albums"].update(album_ids)


GROUPS = [
    ("genre", "Genre", "album", False),
    ("decade", "Release decade", "album", True),
    ("rating", "Rating", "album", True),
    ("release_type", "Release type", "album", False),
    ("country", "Country", "artist", False),
    ("formed", "Formed in", "artist", True),
    ("artist_type", "Artist type", "artist", False),
    (FacetKind.MEMBER, "Band member", "artist", False),
    (FacetKind.INSTRUMENT, "Instrument", "artist", False),
    (FacetKind.STUDIO, "Recording studio", "album", False),
    (FacetKind.PRODUCER, "Producer", "album", False),
    (FacetKind.ENGINEER, "Sound engineer", "album", False),
    (FacetKind.ARTWORK, "Artwork", "album", False),
    (FacetKind.MUSICIAN, "Album musician", "album", False),
    (FacetKind.LABEL, "Label", "album", False),
]
RATING_BANDS = [(4.5, "4.5 – 5"), (4.0, "4.0 – 4.4"), (3.5, "3.5 – 3.9"), (3.0, "3.0 – 3.4"), (0, "below 3")]


def decade_label(year):
    return f"{year // 10 * 10}s" if year else ""


def rating_band(rating):
    """Band of the rating as shown on the page (one decimal, rounded half up like floatformat)."""
    if rating is None:
        return ""
    value = float(Decimal(str(rating)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    for floor, label in RATING_BANDS:
        if value >= floor:
            return label
    return ""


def split_genres(value):
    """Genre names in one tag value ("Rock, Blues" -> ["Rock", "Blues"]); shared with the genre pages."""
    return [part.strip() for part in re.split(r"[,;|]+", value or "") if part.strip()]


def build_index():
    groups = {key: Group(key, label, level, ordered) for key, label, level, ordered in GROUPS}
    albums = list(Album.objects.values("id", "artist_id", "album_genre", "album_publisher", "album_release_year"))
    albums_by_artist = {}
    for album in albums:
        albums_by_artist.setdefault(album["artist_id"], set()).add(album["id"])

    album_profiles = {p.album_id: p for p in AlbumProfile.objects.filter(status="matched")}
    for album in albums:
        ids = {album["id"]}
        profile = album_profiles.get(album["id"])
        for genre in split_genres(album["album_genre"]):
            groups["genre"].add(genre, ids)
        groups[FacetKind.LABEL].add(album["album_publisher"], ids)
        groups["decade"].add(decade_label(album["album_release_year"] or (profile.release_year if profile else None)), ids)
        if profile:
            groups["rating"].add(rating_band(profile.rating), ids)
            kind = profile.secondary_types.split(",")[0].strip() if profile.secondary_types else profile.primary_type
            groups["release_type"].add(kind, ids)

    matched = set()
    for profile in ArtistProfile.objects.filter(status="matched"):
        matched.add(profile.artist_id)
        ids = albums_by_artist.get(profile.artist_id, set())
        groups["country"].add(profile.country, ids)
        groups["formed"].add(decade_label(profile.begin_year), ids)
        groups["artist_type"].add(profile.artist_type, ids)
    # Tag-derived artist facts for artists not matched online.
    for artist in Artist.objects.exclude(pk__in=matched).values("id", "artist_country", "artist_formed_year"):
        ids = albums_by_artist.get(artist["id"], set())
        groups["country"].add(artist["artist_country"], ids)
        groups["formed"].add(decade_label(artist["artist_formed_year"]), ids)

    for link in ArtistFacet.objects.values("artist_id", "facet__kind", "facet__name"):
        group = groups.get(link["facet__kind"])
        if group:
            group.add(link["facet__name"], albums_by_artist.get(link["artist_id"], set()))
    for link in AlbumFacet.objects.values("album_id", "facet__kind", "facet__name"):
        group = groups.get(link["facet__kind"])
        if group:
            group.add(link["facet__name"], {link["album_id"]})

    return [groups[key] for key, *_ in GROUPS], {album["id"] for album in albums}


def parse_selection(raw_values):
    """``["genre:Heavy Metal", ...]`` -> {"genre": {"heavy metal"}}."""
    selection = {}
    for raw in raw_values:
        key, _, value = raw.partition(":")
        if key and value.strip():
            selection.setdefault(key, set()).add(value.strip().casefold())
    return selection


def selection_param(group_key, value_key):
    return f"{group_key}:{value_key}"


@dataclass
class ExploreResult:
    groups: list
    album_ids: set
    selected: list  # [{"group", "group_label", "name", "param"}]


def explore(selection):
    groups, all_ids = build_index()
    by_key = {group.key: group for group in groups}
    selection = {key: values for key, values in selection.items() if key in by_key}

    def matching(group):
        values = selection.get(group.key)
        if not values:
            return None
        ids = set()
        for value in values:
            ids |= group.values.get(value, {}).get("albums", set())
        return ids

    group_matches = {group.key: matching(group) for group in groups}

    def intersect(skip=None):
        ids = set(all_ids)
        for key, matches in group_matches.items():
            if key != skip and matches is not None:
                ids &= matches
        return ids

    result_ids = intersect()
    selected = []
    for group in groups:
        context = intersect(skip=group.key)
        chosen = selection.get(group.key, set())
        rows = []
        for value_key, entry in group.values.items():
            count = len(entry["albums"] & context)
            is_selected = value_key in chosen
            if count or is_selected:
                rows.append({
                    "key": value_key,
                    "name": entry["name"],
                    "count": count,
                    "selected": is_selected,
                    "param": selection_param(group.key, value_key),
                })
            if is_selected:
                selected.append({"group": group.key, "group_label": group.label, "name": entry["name"],
                                 "param": selection_param(group.key, value_key)})
        if group.key == "rating":
            band_order = [label for _, label in RATING_BANDS]
            rows.sort(key=lambda row: band_order.index(row["name"]) if row["name"] in band_order else 99)
        elif group.ordered:
            rows.sort(key=lambda row: row["name"])
        else:
            rows.sort(key=lambda row: (-row["count"], row["name"].casefold()))
        group.rows = rows
        group.selected_count = len(chosen)
    return ExploreResult(groups=groups, album_ids=result_ids, selected=selected)
