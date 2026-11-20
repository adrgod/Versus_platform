import json
import mimetypes
import re
import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, IntegerField, Q, Value
from django.db.models.functions import Cast, Coalesce, Lower, NullIf, Substr, TruncDate
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_POST
from mutagen.easyid3 import EasyID3

from . import enrichment_service, queries, repository
from .models import (
	Album, AlbumCredit, AlbumDocument, AlbumFacet, AlbumInsight, AlbumProfile, Artist, ArtistFacet, TrackLyrics, ArtistProfile, EnrichmentStatus, FacetKind, DiscogsAlbum, LibraryState, MetalArchivesAlbum, MetalArchivesArtist, Track,
)
from .models import AlbumRating, TrackRating
from .reviews import review_context
from .scan_service import is_importing, read_local_tags, start_scan
from .services.files_scanner import (
	calculate_confidence_for_track,
	derived_tags,
	read_artwork_from_id3,
	read_file_metadata,
)
from .services import enrichment as enrichment_api
from .services import discogs as discogs_service
from .services import explorer, fill_missing
from .services import resolver as resolver_api
from .services import insights as insights_api
from .services.gemini import GeminiClient
from .services.ollama import OllamaClient, settings as insight_settings
from .services import tag_editor
from .services.carry_over import import_and_carry_over
from .services.tag_writer import TagSnapshot, batch_files, get_job, plan_file, start_batch_write


def _with_covers(albums):
	albums = list(albums)
	for album in albums:
		album.cover_uri = repository.album_cover_data_uri(album.album_cover)
	return albums


@login_required
def dashboard(request):
	recent_albums = queries.albums().order_by("-when_loaded", Lower("album_name"))[:6]
	stats = queries.dashboard_stats()
	stats["completeness"] = _overall_completeness()
	return render(request, "library/dashboard.html", {
		"state": LibraryState.load(),
		"stats": stats,
		"recent_albums": _with_covers(recent_albums),
	})


@login_required
@require_GET
def folder_browser(request):
	state = LibraryState.load()
	selected_folder = Path(state.source_folder).expanduser().resolve()

	roots = repository.get_allowed_roots()
	messages_to_show = []
	requested = request.GET.get("path")
	try:
		current = repository.resolve_allowed_folder(requested or selected_folder)
	except (OSError, RuntimeError, ValueError):
		current = roots[0] if roots else None
		messages_to_show.append("That folder is unavailable or outside the allowed roots.")

	directories = []
	parent = None
	if current is not None:
		try:
			directories = sorted(
				(item for item in current.iterdir() if item.is_dir() and not item.name.startswith(".")),
				key=lambda item: item.name.casefold(),
			)
		except OSError:
			messages_to_show.append("This folder cannot be listed with the current account.")
		if any(current != root for root in roots) and any(root in current.parents for root in roots):
			parent = current.parent

	return render(request, "library/folder_browser.html", {
		"current": current,
		"directories": directories,
		"parent": parent,
		"roots": roots if len(roots) > 1 else [],  # the switcher (drives) only helps with several
		"selected_folder": selected_folder,
		"messages_to_show": messages_to_show,
	})


@login_required
@require_POST
def choose_folder(request):
	try:
		selected = repository.resolve_allowed_folder(request.POST.get("folder", ""))
		state = LibraryState.load()
		state.source_folder = str(selected)
		state.save(update_fields=["source_folder"])
		messages.success(request, f"Library location set to {selected}")
	except (OSError, RuntimeError, ValueError) as error:
		messages.error(request, str(error))
		return redirect("library:folder-browser")
	return redirect("library:dashboard")


@login_required
@require_POST
def refresh_library(request):
	source_folder = LibraryState.load().source_folder

	try:
		if start_scan(source_folder):
			messages.success(request, "Import Data started. This page shows the result when it finishes.")
		else:
			messages.warning(request, "Import Data is already running.")
	except (OSError, RuntimeError, ValueError) as error:
		messages.error(request, str(error))
	return redirect("library:dashboard")


@login_required
def browse(request):
	data_elements = [
		{
			"name": "Music genre",
			"description": "Explore artists, albums, and tracks by genre.",
			"url": reverse("library:genre-index"),
			"kind": "genre",
		},
		{
			"name": "Tag explorer",
			"description": "Combine genres, countries, decades, musicians, studios and more.",
			"url": reverse("library:explore"),
			"kind": "explore",
		},
		{
			"name": "Decades",
			"description": "Top-rated albums of each decade.",
			"url": reverse("library:decade-index"),
			"kind": "decade",
		},
		{
			"name": "Loaded date",
			"description": "Browse music by the date it entered the database.",
			"url": reverse("library:loaded-date-index"),
			"kind": "loaded-date",
		},
	]
	return render(request, "library/browse.html", {"data_elements": data_elements})


@login_required
def genre_index(request):
	search = request.GET.get("q", "").strip()
	rows = queries.albums().order_by(
		Lower("resolved_genre"), Lower("artist__artist_name"), Lower("album_name")
	)
	genres_by_key = {}
	for row in rows:
		for name in explorer.split_genres(row.genre):
			key = name.casefold()
			if search and search.casefold() not in name.casefold():
				continue
			genre = genres_by_key.setdefault(key, {
				"name": name,
				"album_ids": set(),
				"artist_ids": set(),
				"track_count": 0,
			})
			genre["album_ids"].add(row.album_id)
			genre["artist_ids"].add(row.artist_id)
			genre["track_count"] += row.track_count
	genres = sorted(
		({**genre, "album_count": len(genre["album_ids"]), "artist_count": len(genre["artist_ids"])}
		 for genre in genres_by_key.values()),
		key=lambda genre: genre["name"].casefold(),
	)
	return render(request, "library/genre_index.html", {
		"genres": genres,
		"search": search,
	})


@login_required
def loaded_date_index(request):
	rows = (
		Track.objects
		.annotate(loaded_date=TruncDate("when_loaded"))
		.values("loaded_date")
		.annotate(
			track_count=Count("id"),
			album_count=Count("album", distinct=True),
			artist_count=Count("album__artist", distinct=True),
		)
		.order_by("-loaded_date")
	)
	loaded_dates = [
		{**row, "day": row["loaded_date"], "loaded_date": row["loaded_date"].isoformat()}
		for row in rows
		if row["loaded_date"] is not None
	]
	return render(request, "library/loaded_date_index.html", {"loaded_dates": loaded_dates})


@login_required
def genre_detail(request):
	requested_genre = request.GET.get("name", "").strip()
	if not requested_genre:
		return redirect("library:browse")

	all_albums = queries.albums().order_by(*queries.album_order())
	matching_albums = [
		album for album in all_albums
		if any(name.casefold() == requested_genre.casefold() for name in explorer.split_genres(album.genre))
	]
	if not matching_albums:
		raise Http404("Genre not found")
	genre_name = next(
		name for name in explorer.split_genres(matching_albums[0].genre)
		if name.casefold() == requested_genre.casefold()
	)
	album_ids = [album.album_id for album in matching_albums]
	tracks = queries.tracks().filter(album_id__in=album_ids).order_by(*queries.track_order())

	artists, albums, track_rows = _prepare_browse_detail(matching_albums, tracks)

	return render(request, "library/genre_detail.html", {
		"genre": genre_name,
		"artists": artists,
		"albums": albums,
		"tracks": track_rows,
	})


def _prepare_browse_detail(matching_albums, tracks):
	tracks_by_album = {}
	tracks_by_artist = {}
	artist_info = {
		album.artist_id: {"artist_id": album.artist_id, "artist_name": album.artist_name}
		for album in matching_albums
	}
	track_rows = []
	for track in tracks:
		track.player_url = None
		track_rows.append(track)
		if not track.file_path or not repository.is_allowed_file(track.file_path):
			continue
		player_track = {
			"url": reverse("library:stream-track", args=(track.track_id,)),
			"title": track.track_name,
			"album": track.album_name,
			"artist": track.artist_name,
		}
		track.player_url = player_track["url"]
		tracks_by_album.setdefault(track.album_id, []).append(player_track)
		tracks_by_artist.setdefault(track.artist_id, []).append(player_track)

	albums = _with_covers(matching_albums)
	for album in albums:
		album.queue_tracks = tracks_by_album.get(album.album_id, [])
		album.queue_id = f"album-queue-{album.album_id}"
	artists = sorted(artist_info.values(), key=lambda artist: artist["artist_name"].casefold())
	for artist in artists:
		artist["queue_tracks"] = tracks_by_artist.get(artist["artist_id"], [])
		artist["queue_id"] = f"artist-queue-{artist['artist_id']}"
	return artists, albums, track_rows


@login_required
def loaded_date_detail(request):
	requested_date = request.GET.get("day", "").strip()
	try:
		loaded_date = date.fromisoformat(requested_date)
	except ValueError:
		raise Http404("Loaded date not found")

	loaded_on_day = Q(track__when_loaded__date=loaded_date)
	if not Track.objects.filter(when_loaded__date=loaded_date).exists():
		raise Http404("Loaded date not found")
	matching_albums = queries.albums(loaded_on_day).order_by(
		Lower("artist__artist_name"), Lower("album_name")
	)
	tracks = queries.tracks().filter(when_loaded__date=loaded_date).order_by(*queries.track_order())

	artists, albums, track_rows = _prepare_browse_detail(list(matching_albums), tracks)
	return render(request, "library/loaded_date_detail.html", {
		"loaded_date": loaded_date,
		"artists": artists,
		"albums": albums,
		"tracks": track_rows,
	})


@login_required
def artist_list(request):
	search = request.GET.get("q", "").strip()
	artists = list(queries.artists().filter(artist_name__icontains=search).order_by(Lower("artist_name")))
	ids = [artist.pk for artist in artists]

	# Per-artist facts the filters work on.
	batches, years = {}, {}
	for artist_id, batch, year in Album.objects.filter(artist_id__in=ids).values_list("artist_id", "album_batch_date", "resolved_year"):
		if batch:
			batches.setdefault(artist_id, set()).add(batch.isoformat())
		if year:
			years.setdefault(artist_id, set()).add(str(year))
	countries = dict(Artist.objects.filter(pk__in=ids).exclude(resolved_country="").values_list("pk", "resolved_country"))
	places = dict(Artist.objects.filter(pk__in=ids).exclude(resolved_place="").values_list("pk", "resolved_place"))
	main_genre = {}
	for artist_id, name in (
		ArtistFacet.objects.filter(artist_id__in=ids, facet__kind=FacetKind.GENRE)
		.order_by("-weight", "facet__name").values_list("artist_id", "facet__name")
	):
		main_genre.setdefault(artist_id, name)
	for artist_id, tag in Album.objects.filter(artist_id__in=ids).exclude(resolved_genre="").order_by("resolved_year").values_list("artist_id", "resolved_genre"):
		if artist_id not in main_genre and explorer.split_genres(tag):
			main_genre[artist_id] = explorer.split_genres(tag)[0]
	voice = {}
	for artist_id, instrumental, text in TrackLyrics.objects.filter(
		track__album__artist_id__in=ids, status=EnrichmentStatus.MATCHED
	).values_list("track__album__artist_id", "instrumental", "text"):
		kind = "with voice" if (text and not instrumental) else "instrumental" if instrumental else None
		if kind == "with voice":
			voice[artist_id] = "with voice"
		elif kind and artist_id not in voice:
			voice[artist_id] = "instrumental"

	for artist in artists:
		artist.batches = batches.get(artist.pk, set())
		artist.release_years = years.get(artist.pk, set())
		artist.country = countries.get(artist.pk, "")
		artist.place = places.get(artist.pk, "")
		artist.main_genre = main_genre.get(artist.pk, "")
		artist.voice = voice.get(artist.pk, "")

	def album_bucket(count):
		return "1" if count == 1 else "2-3" if count <= 3 else "4-6" if count <= 6 else "7+"

	filters = [
		("batch", "Batch", lambda a: a.batches),
		("country", "Country", lambda a: {a.country} if a.country else set()),
		("genre", "Main genre", lambda a: {a.main_genre} if a.main_genre else set()),
		("year", "Release year", lambda a: a.release_years),
		("albums", "Number of albums", lambda a: {album_bucket(a.album_count)}),
		("voice", "Voice", lambda a: {a.voice} if a.voice else set()),
	]
	chosen = {key: request.GET.get(key, "").strip() for key, _, _ in filters}
	facets = []
	for key, label, getter in filters:
		values = set().union(*(getter(artist) for artist in artists)) if artists else set()
		if key == "albums":
			ordered = [v for v in ("1", "2-3", "4-6", "7+") if v in values]
		elif key == "year":
			ordered = sorted(values, reverse=True)
		elif key == "batch":
			ordered = sorted(values, reverse=True)
		else:
			ordered = sorted(values, key=str.casefold)
		options = [
			{"value": value, "label": (
				_day_label(date.fromisoformat(value)) if key == "batch"
				else f"{value} albums" if key == "albums" and value != "1" else "1 album" if key == "albums"
				else value.capitalize() if key == "voice" else value
			)}
			for value in ordered
		]
		facets.append({"key": key, "label": label, "options": options, "selected": chosen[key]})
	rows = [
		artist for artist in artists
		if all(not chosen[key] or chosen[key] in getter(artist) for key, _, getter in filters)
	]
	return render(request, "library/artist_list.html", {
		"artists": rows[:500],
		"total": len(rows),
		"search": search,
		"facets": facets,
		"filtered": any(chosen.values()),
	})


def _day_label(day):
	""""5 Oct 2026"; %-d is not understood by strftime on Windows, so the day is written by hand."""
	return f"{day.day} {day:%b %Y}"


def _job_is_about(job, *, artist_id, album_id=None):
	"""True while ``job`` runs for this artist or album, so its page should keep refreshing."""
	if not job or not job.running:
		return False
	album_ids = job.options.get("album_ids") or []
	artist_ids = job.options.get("artist_ids") or []
	if album_id is not None:
		return album_id in album_ids or artist_id in artist_ids
	return artist_id in artist_ids or Album.objects.filter(pk__in=album_ids, artist_id=artist_id).exists()


def _last_notes_run(*, artist_id, album_id=None):
	"""What the last finished Enrich Data with AI run said about this artist or album, for the page to show."""
	job = enrichment_service.get_job("notes")
	if not job or job.running or not job.finished:
		return None
	album_ids = job.options.get("album_ids") or []
	artist_ids = job.options.get("artist_ids") or []
	if album_id is not None:
		about = album_id in album_ids or artist_id in artist_ids or not (album_ids or artist_ids)
	else:
		about = artist_id in artist_ids or not (album_ids or artist_ids) or Album.objects.filter(pk__in=album_ids, artist_id=artist_id).exists()
	if not about:
		return None
	stats = job.stats
	lines = [line for _, line in (stats.report if stats else [])][:12]
	problems = ([job.error] if job.error else []) + ([stats.extraction_skipped] if stats and stats.extraction_skipped else []) \
		+ [error for error in (stats.errors if stats else [])][:5]
	return {"finished": job.finished, "lines": lines, "problems": problems}


@login_required
def artist_detail(request, artist_id):
	artist = get_object_or_404(Artist, pk=artist_id)
	albums = (
		queries.albums()
		.filter(artist_id=artist_id)
		.order_by("resolved_year", Lower("album_name"))
	)
	profile = ArtistProfile.objects.filter(artist=artist).first()
	links = artist.facet_links.select_related("facet").order_by("-weight", "facet__name")
	chips = _facet_chips(links)
	lineup = []
	if profile and profile.status == EnrichmentStatus.MATCHED:
		for member in profile.lineup:
			lineup.append({
				**member,
				"url": facet_url(FacetKind.MEMBER, member["name"]),
				"instrument_links": [
					{"name": name, "url": facet_url(FacetKind.INSTRUMENT, name)} for name in member["instruments"]
				],
			})
	matched = profile is not None and profile.status == EnrichmentStatus.MATCHED
	country = artist.resolved_country
	job = enrichment_service.running_job()
	return render(request, "library/artist_detail.html", {
		"artist": artist,
		"albums": _with_covers(albums),
		"profile": profile,
		"matched": matched,
		"genres": chips.get(FacetKind.GENRE, []),
		"lineup": lineup,
		"discography": profile.discography if matched else [],
		"country": country,
		"place": artist.resolved_place,
		"country_url": facet_url("country", country) if country else None,
		"formed": artist.resolved_formed_year,
		"matched_on": _matched_on(artist=artist),
		"job": job,
		"last_notes": _last_notes_run(artist_id=artist.pk),
		"job_here": _job_is_about(job, artist_id=artist.pk),
		"importing": is_importing(),
		"history": [
			{**event, "cause_url": facet_url(FacetKind.CAUSE, event["cause_label"]) if event["cause_label"] else None}
			for event in insights_api.artist_history(artist)
		],
	})


@login_required
def album_list(request):
	search = request.GET.get("q", "").strip()
	rows = (
		queries.albums()
		.filter(Q(album_name__icontains=search) | Q(artist__artist_name__icontains=search))
		.order_by(*queries.album_order())[:500]
	)
	return render(request, "library/album_list.html", {"albums": _with_covers(rows), "search": search})


CREDIT_CELL_LABELS = [
	("composer", "Main composer", FacetKind.COMPOSER),
	("lyricist", "Main lyricist", FacetKind.LYRICIST),
	("studio", "Recording studio", FacetKind.STUDIO),
	("producer", "Producer", FacetKind.PRODUCER),
	("engineer", "Sound engineer", FacetKind.ENGINEER),
	("artwork", "Artwork artist", FacetKind.ARTWORK),
]


def _credit_cells(resolved_credits):
	"""The fixed Credits cells of an album page, from the credits stored on the album."""
	cells = []
	for key, label, kind in CREDIT_CELL_LABELS:
		cell = resolved_credits.get(key) or {"source": "", "entries": []}
		entries = [{**entry, "url": facet_url(kind, entry["name"])} for entry in cell["entries"]]
		cells.append({"label": label, "entries": entries, "source": resolver_api.source_label(cell["source"]) if entries else ""})
	return cells


@login_required
def album_detail(request, album_id):
	album = get_object_or_404(queries.albums(), pk=album_id)
	album.cover_uri = repository.album_cover_data_uri(album.album_cover)
	tracks = list(
		queries.tracks().filter(album_id=album_id).order_by("track_discnumber", "resolved_number", Lower("track_title"))
	)
	multi_disc = len({track.track_discnumber for track in tracks}) > 1
	playable_tracks = [
		{
			"url": reverse("library:stream-track", args=(track.track_id,)),
			"title": track.track_name,
			"album": album.album_name,
		}
		for track in tracks
		if track.file_path and repository.is_allowed_file(track.file_path)
	]
	profile = AlbumProfile.objects.filter(album_id=album_id).first()
	matched = profile is not None and profile.status == EnrichmentStatus.MATCHED
	chips = _facet_chips(AlbumFacet.objects.filter(album_id=album_id).select_related("facet").order_by("-weight", "facet__name"))
	# Everything in Details and Credits is stored on the album (services/resolver.py); nothing is worked out here.
	sources = {key: resolver_api.source_label(value) for key, value in album.resolved_sources.items()}
	genres = chips.get(FacetKind.GENRE, [])
	genre_names = explorer.split_genres(album.resolved_genre)
	country, label, year = album.resolved_country, album.resolved_label, album.resolved_year
	insight = AlbumInsight.objects.filter(album_id=album_id).first()
	decade = explorer.decade_label(year)
	lyrics_by_track = {item.track_id: item for item in TrackLyrics.objects.filter(track__album_id=album_id)}
	job = enrichment_service.running_job()
	lyric_tracks = [
		{"track": track, "lyrics": lyrics_by_track[track.track_id]}
		for track in tracks
		if track.track_id in lyrics_by_track and (lyrics_by_track[track.track_id].text or lyrics_by_track[track.track_id].instrumental)
	]
	track_ratings = dict(TrackRating.objects.filter(
		user=request.user, track__album_id=album_id).values_list("track_id", "stars"))
	review_info = review_context(request, album, label)
	album_rating = review_info["review_score"]  # the album's rating and its review's score are one value
	return render(request, "library/album_detail.html", {
		**review_info,
		"album": album,
		"track_ratings": track_ratings,
		"album_rating": album_rating,
		"tracks": tracks,
		"multi_disc": multi_disc,
		"player_tracks": playable_tracks,
		"profile": profile,
		"matched": matched,
		"genres": genres,
		"credit_cells": _credit_cells(album.resolved_credits),
		"country": country,
		"place": album.artist.resolved_place,
		"country_url": facet_url("country", country) if country else None,
		"main_genre": genre_names[0] if genre_names else "",
		"main_genre_url": facet_url(FacetKind.GENRE, genre_names[0]) if genre_names else None,
		"other_genres": genre_names[1:],
		"label": label,
		"label_source": sources.get("label", ""),
		"genre_source": sources.get("genre", ""),
		"country_source": sources.get("country", ""),
		"year_source": sources.get("year", ""),
		"label_url": facet_url(FacetKind.LABEL, label) if label else None,
		"year": year,
		"decade": decade,
		"decade_url": facet_url("decade", decade) if decade else None,
		"insight": insight,
		"insight_ready": insight is not None and insight.status == EnrichmentStatus.MATCHED,
		"events": [
			{**event, "label": insights_api.EVENT_TYPES.get(event["type"], ""), "cause_label": insights_api.CAUSES.get(event.get("cause"), "")}
			for event in (insight.events if insight else [])
		],
		"keywords": chips.get(FacetKind.KEYWORD, []),
		"moods": chips.get(FacetKind.MOOD, []),
		"lyric_themes": chips.get(FacetKind.LYRIC_THEME, []),
		"documents": AlbumDocument.objects.filter(album_id=album_id).order_by("file_path"),
		"document_kinds": AlbumDocument.KINDS,
		"lyric_tracks": lyric_tracks,
		"matched_on": _matched_on(album=album),
		"job": job,
		"last_notes": _last_notes_run(artist_id=album.artist_id, album_id=album_id),
		"job_here": _job_is_about(job, artist_id=album.artist_id, album_id=album_id),
		"importing": is_importing(),
	})


@login_required
@require_GET
def stream_track(request, track_id):
	file_path = Track.objects.filter(pk=track_id).values_list("track_file_path", flat=True).first()
	if not file_path or not repository.is_allowed_file(file_path):
		return HttpResponse("Audio file is unavailable.", status=404)

	path = Path(file_path).resolve()
	file_size = path.stat().st_size
	range_header = request.headers.get("Range")
	start, end, status = 0, file_size - 1, 200
	if range_header:
		try:
			unit, value = range_header.split("=", 1)
			if unit != "bytes" or "," in value:
				raise ValueError
			first, last = value.split("-", 1)
			if first:
				start = int(first)
				end = min(int(last), file_size - 1) if last else file_size - 1
			else:
				suffix_length = int(last)
				start = max(file_size - suffix_length, 0)
			if start > end or start >= file_size:
				raise ValueError
			status = 206
		except ValueError:
			return HttpResponse(status=416, headers={"Content-Range": f"bytes */{file_size}"})

	content_type = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
	content_length = end - start + 1

	def content_chunks():
		with path.open("rb") as audio_file:
			audio_file.seek(start)
			remaining = content_length
			while remaining:
				chunk = audio_file.read(min(64 * 1024, remaining))
				if not chunk:
					break
				remaining -= len(chunk)
				yield chunk

	response = StreamingHttpResponse(content_chunks(), status=status, content_type=content_type)
	response["Accept-Ranges"] = "bytes"
	response["Content-Length"] = str(content_length)
	response["Content-Disposition"] = "inline"
	if status == 206:
		response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
	return response


def _quality_rows(search=""):
	rows = (
		queries.tracks()
		.annotate(release_year=F("album__resolved_year"), genre=F("album__resolved_genre"))
		.filter(
			Q(track_title__icontains=search)
			| Q(album__album_name__icontains=search)
			| Q(album__artist__artist_name__icontains=search)
		)
		.order_by(*queries.track_order())[:1000]
	)
	fields = repository.get_config()["quality_attributes"]
	result = []
	for row in rows:
		row.missing_fields = _missing_quality_fields(row, fields)
		row.quality_percent = (
			round(100 * (len(fields) - len(row.missing_fields)) / len(fields)) if fields else 100
		)
		result.append(row)
	return result


def _overall_completeness():
	"""Average completeness of all tracks in percent (what the Completeness page shows), or None."""
	fields = repository.get_config()["quality_attributes"]
	tracks = queries.tracks().annotate(release_year=F("album__resolved_year"), genre=F("album__resolved_genre"))
	total = count = 0
	for track in tracks:
		total += _track_percent(_missing_quality_fields(track, fields), fields)
		count += 1
	return _percent(total, count) if count else None


def _missing_quality_fields(row, fields):
	row_attributes = {
		"artist": "artist_name",
		"album": "album_name",
		"title": "track_name",
		"tracknumber": "number",
		"date": "release_year",
		"genre": "genre",
	}
	return [field for field in fields if not getattr(row, row_attributes.get(field, field))]


@login_required
def quality_report(request):
	search = request.GET.get("q", "").strip()
	tracks = _quality_rows(search)
	return render(request, "library/quality_report.html", {
		"tracks": tracks,
		"search": search,
		"incomplete_count": sum(bool(track.missing_fields) for track in tracks),
		"average_score": round(sum(track.quality_percent for track in tracks) / len(tracks)) if tracks else 0,
	})


COMPLETENESS_GROUPINGS = [
	("batch", "Batch"),
	("label", "Label"),
	("country", "Country"),
	("decade", "Decade"),
	("year", "Year"),
	("genre", "Main genre"),
]


def _percent(total, count):
	return round(total / count) if count else 0


def _track_percent(missing, fields):
	return round(100 * (len(fields) - len(missing)) / len(fields)) if fields else 100


def _completeness_group_key(by, album):
	"""(sort key, label, date) of the group an album belongs to for the chosen grouping."""
	if by == "batch":
		day = album.album_batch_date
		return (day is None, -(day.toordinal()) if day else 0), (f"Batch {_day_label(day)}" if day else "No batch"), day
	year = album.resolved_year
	if by in ("decade", "year") and year:
		value = explorer.decade_label(year) if by == "decade" else str(year)
		return (False, -(year // 10 * 10 if by == "decade" else year)), value, None
	if by == "label":
		value = album.resolved_label
	elif by == "country":
		value = album.resolved_country
	elif by == "genre":
		value = (explorer.split_genres(album.resolved_genre) or [""])[0]
	else:
		value = ""
	if not value:
		return (True, ""), "Unknown", None
	return (False, value.casefold()), value, None


@login_required
def completeness_report(request):
	fields = repository.get_config()["quality_attributes"]
	by = request.GET.get("by", "batch")
	if by not in dict(COMPLETENESS_GROUPINGS):
		by = "batch"
	tracks_by_album = {}
	tracks = (
		queries.tracks()
		.select_related("album", "album__artist")
		.annotate(release_year=F("album__resolved_year"), genre=F("album__resolved_genre"))
		.order_by(*queries.track_order())
	)
	for track in tracks:
		tracks_by_album.setdefault(track.album_id, []).append(track)

	def new_node(**extra):
		return {"album_count": 0, "complete_count": 0, "incomplete_count": 0, "percent_sum": 0, "track_count": 0, **extra}

	groups = {}
	complete_count = incomplete_count = 0
	overall = new_node()
	albums = Album.objects.select_related("artist", "profile").order_by(
		Lower("artist__artist_name"), Lower("album_name")
	)
	for album in albums:
		sort_key, label, day = _completeness_group_key(by, album)
		group = groups.setdefault(label, new_node(
			sort_key=sort_key, label=label, date=day, artists={},
			folder=(f"folder {day:%Y%m%d}" if day else "files outside a YYYYMMDD folder") if by == "batch" else "",
		))
		artist = group["artists"].setdefault(album.artist_id, new_node(artist=album.artist, albums=[]))
		album_tracks = tracks_by_album.get(album.pk, [])
		track_rows = []
		missing_fields = set()
		percent_sum = 0
		for track in album_tracks:
			track_missing = _missing_quality_fields(track, fields)
			missing_fields.update(track_missing)
			percent = _track_percent(track_missing, fields)
			percent_sum += percent
			track_rows.append({
				"id": track.track_id,
				"name": track.track_name or "Untitled track",
				"missing_fields": track_missing,
				"percent": percent,
			})
		if not album_tracks:
			missing_fields.add("tracks")
		is_complete = bool(album_tracks) and not missing_fields
		artist["albums"].append({
			"album": album,
			"tracks": track_rows,
			"missing_fields": sorted(missing_fields),
			"complete": is_complete,
			"percent": _percent(percent_sum, len(album_tracks)),
		})
		for node in (group, artist, overall):
			node["album_count"] += 1
			node["complete_count" if is_complete else "incomplete_count"] += 1
			node["percent_sum"] += percent_sum
			node["track_count"] += len(album_tracks)
		if is_complete:
			complete_count += 1
		else:
			incomplete_count += 1

	rows = []
	for group in sorted(groups.values(), key=lambda item: item["sort_key"]):
		group["artists"] = sorted(group["artists"].values(), key=lambda item: item["artist"].artist_name.casefold())
		for node in (group, *group["artists"]):
			node["percent"] = _percent(node["percent_sum"], node["track_count"])
		rows.append(group)
	return render(request, "library/completeness_report.html", {
		"batches": rows,
		"groups": rows,
		"by": by,
		"groupings": COMPLETENESS_GROUPINGS,
		"group_label": dict(COMPLETENESS_GROUPINGS)[by],
		"album_count": complete_count + incomplete_count,
		"complete_count": complete_count,
		"incomplete_count": incomplete_count,
		"overall_percent": _percent(overall["percent_sum"], overall["track_count"]),
		"fill_job": enrichment_service.get_job("fill"),
		"running": enrichment_service.running_job(),
		"fillable_count": len(fill_missing.fillable_album_ids()),
	})


@login_required
@require_POST
def fill_missing_start(request):
	"""Fill in Missing Data: local sources first, then an online lookup, for albums missing a year or genre."""
	if enrichment_service.start_job("fill"):
		messages.success(request, "Fill in Missing Data started.")
	else:
		_job_busy(request)
	return redirect("library:completeness-report")


def _tag_fields():
	config = repository.get_config()
	return [field for field in config["file_attributes"] if field != "length"]


def _easyid3_values(file_path, fields):
	tags = EasyID3(file_path)
	return {field: ", ".join(tags.get(field) or []) for field in fields}


def _tag_form(fields, *, data=None, initial=None):
	form = forms.Form(data=data)
	for field in fields:
		form.fields[field] = forms.CharField(
			required=False,
			label=field.replace("_", " ").title(),
			initial=(initial or {}).get(field, ""),
			widget=forms.TextInput(attrs={"autocomplete": "off"}),
		)
	return form


def _get_track_for_edit(track_id):
	track = get_object_or_404(queries.tracks(), pk=track_id)
	if not track.file_path or not repository.is_allowed_file(track.file_path):
		raise ValueError("This track has no usable path. Refresh the library before editing tags.")
	return track


@login_required
def edit_track_tags(request, track_id):
	try:
		track = _get_track_for_edit(track_id)
	except ValueError as error:
		messages.error(request, str(error))
		return redirect("library:quality-report")
	path = Path(track.file_path).resolve()
	fields = _tag_fields()
	try:
		current_tags = _easyid3_values(path, fields)
	except Exception as error:
		messages.error(request, f"Could not read ID3 tags: {error}")
		return redirect("library:quality-report")

	if request.method == "POST":
		form = _tag_form(fields, data=request.POST)
		if form.is_valid():
			proposed = {field: form.cleaned_data[field].strip() for field in fields}
			changes = [
				{"field": field, "before": current_tags[field], "after": proposed[field]}
				for field in fields if current_tags[field] != proposed[field]
			]
			stat = path.stat()
			token = signing.dumps({
				"track_id": track_id,
				"tags": proposed,
				"mtime_ns": stat.st_mtime_ns,
				"size": stat.st_size,
			}, salt="library.id3-preview")
			return render(request, "library/tag_preview.html", {
				"track": track,
				"changes": changes,
				"preview_token": token,
			})
	else:
		form = _tag_form(fields, initial=current_tags)

	return render(request, "library/tag_edit.html", {"track": track, "form": form})


@login_required
@require_POST
def confirm_tag_edit(request, track_id):
	token = request.POST.get("preview_token", "")
	try:
		payload = signing.loads(token, salt="library.id3-preview", max_age=900)
		if payload["track_id"] != track_id:
			raise signing.BadSignature("Preview does not match this track")
		track = _get_track_for_edit(track_id)
		path = Path(track.file_path).resolve()
		original_stat = path.stat()
		if (original_stat.st_mtime_ns, original_stat.st_size) != (payload["mtime_ns"], payload["size"]):
			raise ValueError("The MP3 changed after the preview. Reload the editor and review again.")
	except (signing.BadSignature, KeyError, OSError, ValueError) as error:
		messages.error(request, str(error) or "The preview expired. Please review the tags again.")
		return redirect("library:edit-tags", track_id=track_id)

	fields = _tag_fields()
	snapshot = TagSnapshot(path)  # original tag block, in memory only

	try:
		tags = EasyID3(path)
		for field in fields:
			value = payload["tags"].get(field, "")
			if value:
				tags[field] = [value]
			elif field in tags:
				del tags[field]
		tags.save()

		verified = _easyid3_values(path, fields)
		if any(verified[field] != payload["tags"].get(field, "") for field in fields):
			raise ValueError("The written tags did not verify; the backup will be restored.")

		record = read_file_metadata(path, repository.get_config())
		record["album_cover"] = read_artwork_from_id3(path)
		cast(dict[str, object], record)["dq_confidence"] = calculate_confidence_for_track(record, record["file_path"])

		# The tags may change which artist/album/track this is: the old row is then
		# replaced, its details are carried to the new entry, and anything left empty is pruned. If the new tags cannot be
		# stored the whole change rolls back and the file is restored below.
		with transaction.atomic():
			stats, _ = import_and_carry_over([record])
			if stats["skipped"]:
				raise ValueError("Artist, album and title cannot be empty; the original file will be restored.")
	except Exception as error:
		snapshot.restore()
		messages.error(request, f"Could not save tags; the original tags were put back: {error}")
		return redirect("library:edit-tags", track_id=track_id)

	messages.success(request, "Tags saved and verified.")
	return redirect("library:album-list")



def _parse_batch_day(day):
	try:
		return datetime.strptime(day, "%Y%m%d").date()
	except ValueError:
		raise Http404("Unknown batch") from None


@login_required
def batch_index(request):
	batches = (
		Album.objects.filter(album_batch_date__isnull=False)
		.values("album_batch_date")
		.annotate(
			album_count=Count("id", distinct=True),
			artist_count=Count("artist", distinct=True),
			track_count=Count("track"),
		)
		.order_by("-album_batch_date")
	)
	return render(request, "library/batch_index.html", {"batches": batches})


@login_required
def batch_detail(request, day):
	batch_date = _parse_batch_day(day)
	albums = queries.albums().filter(album_batch_date=batch_date).order_by(*queries.album_order())
	if not albums:
		raise Http404("Unknown batch")
	return render(request, "library/batch_detail.html", {
		"day": day,
		"batch_date": batch_date,
		"albums": albums,
		"artist_count": len({album.artist_id for album in albums}),
		"job": get_job(batch_date),
	})


@login_required
def batch_preview(request, day):
	batch_date = _parse_batch_day(day)
	image_cache = {}
	rows, errors, counts = [], [], {}
	files = batch_files(batch_date)
	for path in files:
		try:
			plan = plan_file(path, image_cache)
		except Exception as error:
			errors.append(f"{path}: {error}")
			continue
		if plan is None or plan.is_empty:
			continue
		for name in plan.changes:
			counts[name] = counts.get(name, 0) + 1
		if plan.cover:
			counts["cover"] = counts.get("cover", 0) + 1
		rows.append({
			"path": path,
			"changes": [{"field": name, "value": value} for name, value in plan.changes.items()]
			+ ([{"field": "cover", "value": "image from the album folder"}] if plan.cover else []),
		})
	return render(request, "library/batch_preview.html", {
		"day": day,
		"batch_date": batch_date,
		"file_count": len(files),
		"rows": rows[:200],
		"more_rows": max(len(rows) - 200, 0),
		"changed_count": len(rows),
		"counts": sorted(counts.items()),
		"errors": errors[:30],
		"running": bool(get_job(batch_date) and get_job(batch_date).running),
	})


@login_required
@require_POST
def batch_write(request, day):
	batch_date = _parse_batch_day(day)
	if not start_batch_write(batch_date):
		messages.warning(request, "This batch is already being written.")
	return redirect("library:batch-status", day=day)


@login_required
def batch_status(request, day):
	batch_date = _parse_batch_day(day)
	job = get_job(batch_date)
	return render(request, "library/batch_status.html", {"day": day, "batch_date": batch_date, "job": job})


# ---------------------------------------------------------------------------
# Online enrichment and tag explorer
# ---------------------------------------------------------------------------


EXPLORE_SORTS = {
	"year": ("Year", [F("sort_year").asc(nulls_last=True), Lower("artist__artist_name")]),
	"rating": ("Rating", [F("rating").desc(nulls_last=True), F("rating_votes").desc(), Lower("album_name")]),
	"artist": ("Artist", [Lower("artist__artist_name"), F("sort_year").asc(nulls_last=True)]),
	"title": ("Title", [Lower("album_name")]),
}


def explore_url(params=(), sort=None):
	query = [("f", param) for param in params]
	if sort and sort != "year":
		query.append(("sort", sort))
	url = reverse("library:explore")
	return f"{url}?{urlencode(query)}" if query else url


def facet_url(kind, name):
	return explore_url([explorer.selection_param(kind, name.casefold())])


def _enriched_albums(queryset):
	return queryset.annotate(
		rating=F("profile__rating"),
		rating_votes=F("profile__rating_votes"),
		sort_year=F("resolved_year"),
	)


@login_required
def explore(request):
	sort = request.GET.get("sort", "year")
	if sort not in EXPLORE_SORTS:
		sort = "year"
	result = explorer.explore(explorer.parse_selection(request.GET.getlist("f")))
	current = [item["param"] for item in result.selected]

	for group in result.groups:
		for row in group.rows:
			params = [p for p in current if p != row["param"]] if row["selected"] else current + [row["param"]]
			row["url"] = explore_url(params, sort)
		# The first ten values, plus any ticked value further down, stay visible.
		group.top_rows = [row for index, row in enumerate(group.rows) if index < 10 or row["selected"]]
		group.more_rows = [row for index, row in enumerate(group.rows) if index >= 10 and not row["selected"]]
	for item in result.selected:
		item["remove_url"] = explore_url([p for p in current if p != item["param"]], sort)

	albums = _enriched_albums(queries.albums().filter(id__in=result.album_ids)).order_by(*EXPLORE_SORTS[sort][1])
	page = Paginator(albums, 48).get_page(request.GET.get("page"))
	page_albums = _with_covers(page.object_list)

	artist_rows = {}
	album_artist_ids = Album.objects.filter(id__in=result.album_ids).values_list("artist_id", flat=True)
	for artist_id in album_artist_ids:
		artist_rows.setdefault(artist_id, 0)
		artist_rows[artist_id] += 1
	artists = []
	for artist in Artist.objects.filter(id__in=artist_rows).select_related("profile").order_by(Lower("artist_name")):
		profile = getattr(artist, "profile", None)
		matched = profile is not None and profile.status == EnrichmentStatus.MATCHED
		artists.append({
			"id": artist.pk,
			"name": artist.artist_name,
			"country": artist.resolved_country,
			"formed": artist.resolved_formed_year,
			"album_count": artist_rows[artist.pk],
		})

	page_query = [("f", param) for param in current] + ([("sort", sort)] if sort != "year" else [])
	return render(request, "library/explore.html", {
		"groups": [group for group in result.groups if group.rows],
		"selected": result.selected,
		"clear_url": explore_url(sort=sort),
		"albums": page_albums,
		"page": page,
		"page_query": urlencode(page_query),
		"album_total": len(result.album_ids),
		"artists": artists,
		"sorts": [{"key": key, "label": label, "url": explore_url(current, key), "active": key == sort}
				  for key, (label, _) in EXPLORE_SORTS.items()],
		"has_profiles": ArtistProfile.objects.filter(status=EnrichmentStatus.MATCHED).exists(),
	})


@login_required
def decade_index(request):
	albums = _enriched_albums(queries.albums()).filter(sort_year__isnull=False)
	by_decade = {}
	for album in albums:
		by_decade.setdefault(album.sort_year // 10 * 10, []).append(album)
	decades = []
	largest = max((len(items) for items in by_decade.values()), default=1)
	for start in sorted(by_decade):
		items = by_decade[start]
		rated = sorted((a for a in items if a.rating is not None),
					   key=lambda a: (-a.rating, -(a.rating_votes or 0), a.album_name.casefold()))
		label = explorer.decade_label(start)
		top = _with_covers(rated[:10])
		decades.append({
			"label": label,
			"start": start,
			"end": start + 9,
			"album_count": len(items),
			"artist_count": len({a.artist_id for a in items}),
			"rated_count": len(rated),
			"average": round(sum(float(a.rating) for a in rated) / len(rated), 2) if rated else None,
			"bar": round(100 * len(items) / largest),
			"top": top,
			"explore_url": explore_url([explorer.selection_param("decade", label.casefold())], "rating"),
		})
	return render(request, "library/decade_index.html", {
		"decades": decades,
		"undated_count": queries.albums().count() - albums.count(),
	})


def _status_counts(model):
	counts = dict(model.objects.values_list("status").annotate(n=Count("id")))
	return {
		"matched": counts.get(EnrichmentStatus.MATCHED, 0),
		"not_found": counts.get(EnrichmentStatus.NOT_FOUND, 0),
		"error": counts.get(EnrichmentStatus.ERROR, 0),
	}


@login_required
def enrichment_status(request):
	artist_counts = _status_counts(ArtistProfile)
	album_counts = _status_counts(AlbumProfile)
	artist_counts["pending"] = Artist.objects.count() - sum(artist_counts.values())
	album_counts["pending"] = Album.objects.count() - sum(album_counts.values())

	unmatched_artists = (
		ArtistProfile.objects.filter(status__in=[EnrichmentStatus.NOT_FOUND, EnrichmentStatus.ERROR])
		.select_related("artist").order_by(Lower("artist__artist_name"))
	)
	doubtful_artists = (
		ArtistProfile.objects.filter(status=EnrichmentStatus.MATCHED, match_score__lt=Decimal("0.80"))
		.select_related("artist").order_by("match_score")
	)
	unmatched_albums = (
		AlbumProfile.objects.filter(status=EnrichmentStatus.NOT_FOUND, album__artist__profile__status=EnrichmentStatus.MATCHED)
		.select_related("album", "album__artist", "album__artist__profile").order_by(Lower("album__artist__artist_name"), Lower("album__album_name"))
	)
	extracted_albums = (
		AlbumInsight.objects.filter(status=EnrichmentStatus.MATCHED)
		.select_related("album", "album__artist")
		.order_by(Lower("album__artist__artist_name"), Lower("album__album_name"))
	)
	failed_albums = (
		AlbumInsight.objects.filter(status=EnrichmentStatus.ERROR)
		.select_related("album", "album__artist")
		.order_by(Lower("album__artist__artist_name"), Lower("album__album_name"))
	)
	gemini = insight_settings().get("provider") == "gemini"
	ollama = GeminiClient() if gemini else OllamaClient()
	return render(request, "library/enrichment.html", {
		"artist_counts": artist_counts,
		"album_counts": album_counts,
		"unmatched_artists": unmatched_artists[:300],
		"doubtful_artists": doubtful_artists[:100],
		"unmatched_albums": unmatched_albums[:300],
		"extracted_albums": extracted_albums,
		"failed_albums": failed_albums,
		"job": enrichment_service.get_job("musicbrainz"),
		"notes_job": enrichment_service.get_job("notes"),
		"metal_job": enrichment_service.get_job("metal"),
		"discogs_job": enrichment_service.get_job("discogs"),
		"discogs_token_set": bool(discogs_service.configured_token()),
		"running": enrichment_service.running_job(),
		"notes_counts": {
			"albums": Album.objects.count(),
			"with_notes": Album.objects.filter(documents__isnull=False).distinct().count(),
			"tracks": Track.objects.count(),
			"with_lyrics": TrackLyrics.objects.filter(status=EnrichmentStatus.MATCHED).count(),
			"extracted": AlbumInsight.objects.filter(status=EnrichmentStatus.MATCHED).count(),
			"failed": AlbumInsight.objects.filter(status=EnrichmentStatus.ERROR).count(),
		},
		"ollama": {**ollama.status(), "url": ollama.url, "model": ollama.model, "gemini": gemini},
	})


def _job_busy(request):
	job = enrichment_service.running_job()
	label = job.label if job else "Another job"
	messages.warning(request, f"{label} is already running. Try again when it finishes.")


def _redirect_to_scope(album_id="", artist_id=""):
	if str(album_id).isdigit():
		return redirect("library:album-detail", album_id=int(album_id))
	if str(artist_id).isdigit():
		return redirect("library:artist-detail", artist_id=int(artist_id))
	return redirect("library:enrichment")


@login_required
@require_POST
def enrichment_start(request):
	"""Lookup MusicBrainz for the library, one artist or one album."""
	album_id, artist_id = request.POST.get("album_id", ""), request.POST.get("artist_id", "")
	album_ids = [int(album_id)] if album_id.isdigit() else None
	artist_ids = [int(artist_id)] if artist_id.isdigit() and not album_ids else None
	if enrichment_service.start_enrichment(
		refresh=request.POST.get("refresh") == "1", artist_ids=artist_ids, album_ids=album_ids
	):
		messages.success(request, "Lookup MusicBrainz started.")
	else:
		_job_busy(request)
	return _redirect_to_scope(album_id, artist_id)


def _matched_on(*, artist=None, album=None):
	"""Where this artist or album was already looked up online: {"found": [{"name", "url"}], "missed": [name]}.

	Found sources are shown as links to the page they matched; sources that were searched
	without a match are only named.
	"""
	found, missed = [], []

	def note(name, entry, url=None):
		if entry is None:
			return
		url = entry.url if url is None else url
		if entry.status == EnrichmentStatus.MATCHED and url:
			found.append({"name": name, "url": url})
		elif entry.status == EnrichmentStatus.NOT_FOUND:
			missed.append(name)

	if album is not None:
		profile = AlbumProfile.objects.filter(album=album).first()
		note("MusicBrainz", profile, f"https://musicbrainz.org/release-group/{profile.external_id}" if profile and profile.external_id else "")
		note("Metal Archives", MetalArchivesAlbum.objects.filter(album=album).first())
		note("Discogs", DiscogsAlbum.objects.filter(album=album).first())
	else:
		profile = ArtistProfile.objects.filter(artist=artist).first()
		note("MusicBrainz", profile, f"https://musicbrainz.org/artist/{profile.external_id}" if profile and profile.external_id else "")
		note("Metal Archives", MetalArchivesArtist.objects.filter(artist=artist).first())
	return {"found": found, "missed": missed} if found or missed else None


@login_required
@require_POST
def metal_start(request):
	"""Lookup MetalArchives for all artists, one artist or one album."""
	album_id, artist_id = request.POST.get("album_id", ""), request.POST.get("artist_id", "")
	options = {"refresh": request.POST.get("refresh") == "1"}
	if album_id.isdigit():
		options["album_ids"] = [int(album_id)]
	elif artist_id.isdigit():
		options["artist_ids"] = [int(artist_id)]
	if enrichment_service.start_job("metal", **options):
		messages.success(request, "Lookup MetalArchives started.")
	else:
		_job_busy(request)
	return _redirect_to_scope(album_id, artist_id)


@login_required
@require_POST
def discogs_start(request):
	"""Lookup Discogs for all albums, one artist's albums or one album."""
	album_id, artist_id = request.POST.get("album_id", ""), request.POST.get("artist_id", "")
	options = {"refresh": request.POST.get("refresh") == "1"}
	if album_id.isdigit():
		options["album_ids"] = [int(album_id)]
	elif artist_id.isdigit():
		options["artist_ids"] = [int(artist_id)]
	if not discogs_service.configured_token():
		messages.error(request, 'Lookup Discogs needs a free access token: set the DISCOGS_TOKEN environment variable (or "discogs_token" under "enrichment" in config.json; create one at discogs.com/settings/developers).')
	elif enrichment_service.start_job("discogs", **options):
		messages.success(request, "Lookup Discogs started.")
	else:
		_job_busy(request)
	return _redirect_to_scope(album_id, artist_id)


@login_required
@require_POST
def enrichment_stop(request):
	kind = request.POST.get("job", "musicbrainz")
	if enrichment_service.stop_job(kind):
		messages.info(request, f"{enrichment_service.JOB_LABELS.get(kind, 'The job')} will stop after the current item.")
	return redirect("library:enrichment")


@login_required
@require_POST
def document_include(request, document_id):
	"""Tick or untick "Include for AI enrichment" for one liner-notes file."""
	document = get_object_or_404(AlbumDocument, pk=document_id)
	try:
		data = json.loads(request.body or b"{}")
		changes = {}
		if "include" in data:
			changes["use_for_ai"] = bool(data["include"])
		if "title" in data:
			changes["title"] = str(data["title"]).strip()[:200]
		if "kind" in data:
			if data["kind"] not in dict(AlbumDocument.KINDS):
				return HttpResponseBadRequest("Unknown kind")
			changes["kind"] = data["kind"]
	except (ValueError, AttributeError, TypeError):
		return HttpResponseBadRequest("Invalid request")
	for name, value in changes.items():
		setattr(document, name, value)
	document.save(update_fields=list(changes) or None)
	return JsonResponse({"ok": True, "include": document.use_for_ai, "title": document.title, "kind": document.kind})


@login_required
@require_POST
def notes_start(request):
	"""Enrich Data with AI: read liner notes and lyrics, then extract facts, for all albums, one artist or one album."""
	options = {
		"refresh": request.POST.get("refresh") == "1",
		"extract": request.POST.get("extract", "1") == "1",
		"online_lyrics": request.POST.get("online_lyrics", "1") == "1",
	}
	album_id, artist_id = request.POST.get("album_id", ""), request.POST.get("artist_id", "")
	if album_id.isdigit():
		options["album_ids"] = [int(album_id)]
	elif artist_id.isdigit():
		options["artist_ids"] = [int(artist_id)]
	if enrichment_service.start_job("notes", **options):
		messages.success(request, "Enrich Data with AI started.")
	else:
		_job_busy(request)
	return _redirect_to_scope(album_id, artist_id)


@login_required
@require_POST
def read_local_tags_view(request):
	"""Read Local MP3 Tags again for one album or one artist, then return to its page."""
	album_id, artist_id = request.POST.get("album_id", ""), request.POST.get("artist_id", "")
	if album_id.isdigit():
		scope = {"album_ids": [int(album_id)]}
	elif artist_id.isdigit():
		scope = {"artist_ids": [int(artist_id)]}
	else:
		messages.error(request, "Choose an artist or an album for Read Local MP3 Tags.")
		return redirect("library:dashboard")
	try:
		result = read_local_tags(**scope)
	except (OSError, ValueError) as error:
		messages.error(request, f"Read Local MP3 Tags failed: {error}")
		return _redirect_to_scope(album_id, artist_id)
	if result is None:
		messages.warning(request, "Import Data is running. Try Read Local MP3 Tags again when it finishes.")
		return _redirect_to_scope(album_id, artist_id)

	changed = result.updated + result.replaced
	parts = [f"{result.files_read} file{pluralize(result.files_read)} read"]
	parts.append(f"{changed} track{pluralize(changed)} updated" if changed else "no changes")
	if result.missing:
		parts.append(f"{len(result.missing)} file{pluralize(len(result.missing))} not found (Import Data tidies these up)")
	if result.errors:
		parts.append(f"{len(result.errors)} file{pluralize(len(result.errors))} could not be read")
	moved = [
		f"the new {noun}" if count == 1 else f"{count} new {noun}s" for count, noun in
		((result.albums_carried, "album"), (result.artists_carried, "artist")) if count
	]
	if moved:
		parts.append(f"details carried over to {' and '.join(moved)}")
	level = messages.warning if result.missing or result.errors else messages.success
	level(request, "Read Local MP3 Tags: " + " · ".join(parts) + ".")

	# A big tag change can move the tracks to a new album/artist row; follow them there.
	if album_id.isdigit() and not Album.objects.filter(pk=int(album_id)).exists():
		new_album = Track.objects.filter(pk__in=result.track_ids).values_list("album_id", flat=True).first()
		return redirect("library:album-detail", album_id=new_album) if new_album else redirect("library:album-list")
	if artist_id.isdigit() and not Artist.objects.filter(pk=int(artist_id)).exists():
		new_artist = Track.objects.filter(pk__in=result.track_ids).values_list("album__artist_id", flat=True).first()
		return redirect("library:artist-detail", artist_id=new_artist) if new_artist else redirect("library:artist-list")
	return _redirect_to_scope(album_id, artist_id)


@login_required
@require_POST
def set_external_id(request, kind, object_id):
	try:
		if kind == "artist":
			artist = get_object_or_404(Artist, pk=object_id)
			enrichment_api.set_artist_id(artist, request.POST.get("external_id", ""))
		else:
			album = get_object_or_404(Album, pk=object_id)
			artist = album.artist
			enrichment_api.set_album_id(album, request.POST.get("external_id", ""))
	except ValueError as error:
		messages.error(request, str(error))
		return redirect("library:enrichment")
	if enrichment_service.start_enrichment(artist_ids=[artist.pk]):
		messages.success(request, f"MusicBrainz ID saved; Lookup MusicBrainz started for {artist.artist_name}.")
	else:
		messages.warning(request, "MusicBrainz ID saved. It is used the next time Lookup MusicBrainz runs (another job is running now).")
	return redirect("library:enrichment")


def _facet_chips(links):
	chips = {}
	for link in links:
		facet = link.facet
		chips.setdefault(facet.kind, {})
		key = facet.name.casefold()
		if key not in chips[facet.kind]:
			chips[facet.kind][key] = {"name": facet.name, "detail": link.detail, "url": facet_url(facet.kind, facet.name)}
	return {kind: list(values.values()) for kind, values in chips.items()}


# ---------------------------------------------------------------------------
# Edit Tags: album and artist editors that write to the MP3 files
# ---------------------------------------------------------------------------

EDIT_SALT = "library.edit-tags"


def _file_stamp(path):
	stat = Path(path).stat()
	return [stat.st_mtime_ns, stat.st_size]


def _edit_token(kind, object_id, tracks):
	files = {str(track.pk): [track.track_file_path, _file_stamp(track.track_file_path)] for track in tracks}
	return signing.dumps({"kind": kind, "id": object_id, "files": files}, salt=EDIT_SALT)


def _check_edit_token(request, kind, object_id):
	"""{track pk: path} from the signed form token; raises ValueError when stale or tampered."""
	try:
		payload = signing.loads(request.POST.get("edit_token", ""), salt=EDIT_SALT, max_age=6 * 3600)
	except signing.BadSignature as error:
		raise ValueError("This editor expired. Open Edit Tags again.") from error
	if payload.get("kind") != kind or payload.get("id") != object_id:
		raise ValueError("This form belongs to another page. Open Edit Tags again.")
	files = {}
	for pk, (path, stamp) in payload["files"].items():
		if not repository.is_allowed_file(path) or _file_stamp(path) != stamp:
			raise ValueError(f"{Path(path).name} changed since the editor was opened. Reload Edit Tags and try again.")
		files[int(pk)] = path
	return files


def _shared_value(values):
	"""(value, mixed) for one tag across several files."""
	distinct = sorted({value for value in values})
	if len(distinct) <= 1:
		return (distinct[0] if distinct else ""), False
	return "", True


def _save_message(request, result, noun):
	if not result.files_written:
		messages.info(request, "No changes to save.")
		return
	moved = [
		f"the new {name}" if count == 1 else f"{count} new {name}s" for count, name in
		((result.albums_carried, "album"), (result.artists_carried, "artist")) if count
	]
	text = f"Edit Tags: saved to {result.files_written} file{pluralize(result.files_written)}"
	if moved:
		text += f" · details carried over to {' and '.join(moved)}"
	messages.success(request, text + ".")


def _readable_tracks(queryset):
	tracks = list(queryset.order_by("track_discnumber", "track_number", Lower("track_title")))
	for track in tracks:
		track.readable = bool(track.track_file_path) and repository.is_allowed_file(track.track_file_path)
		track.tags = tag_editor.read_tags(track.track_file_path) if track.readable else {}
	return tracks


def _found_album_values(album):
	"""{tag key: (value, origin)} for values lookups or notes found that the files do not carry."""
	sources = album.resolved_sources
	found = {}

	def add(key, value, source):
		if value and source and source != "tags":
			found[key] = (str(value), resolver_api.source_label(source))

	add("date", album.resolved_year, sources.get("year"))
	add("genre", album.resolved_genre, sources.get("genre"))
	add("publisher", album.resolved_label, sources.get("label"))
	for key, cell in (("composer", "composer"), ("lyricist", "lyricist")):
		credit = album.resolved_credits.get(cell) or {}
		if credit.get("entries"):
			add(key, credit["entries"][0]["name"], credit.get("source"))
	return found


@login_required
def album_edit_tags(request, album_id):
	"""Edit Tags for one album: album tags in a header, every track's tags in a table below."""
	album = get_object_or_404(Album.objects.select_related("artist"), pk=album_id)
	tracks = _readable_tracks(Track.objects.filter(album=album))
	readable = [track for track in tracks if track.readable]
	errors, posted = {}, None

	if request.method == "POST":
		try:
			files = _check_edit_token(request, "album", album.pk)
		except ValueError as error:
			messages.error(request, str(error))
			return redirect("library:album-edit-tags", album_id=album.pk)
		posted = request.POST
		by_pk = {track.pk: track for track in readable if track.pk in files}
		edits = {}
		for track in by_pk.values():
			values = {key: posted.get(f"t{track.pk}-{key}", track.tags.get(key, "")) for key, _ in tag_editor.TRACK_FIELDS}
			edits[track.track_file_path] = values
		for key, _ in tag_editor.ALBUM_FIELDS:
			header, initial = posted.get(f"h-{key}", ""), posted.get(f"h-{key}-initial", "")
			if header.strip() == initial.strip():
				continue  # the header was not touched
			for track in by_pk.values():
				values = edits[track.track_file_path]
				cell_untouched = key not in values or values[key].strip() == track.tags.get(key, "")
				if cell_untouched:
					values[key] = header
		for track in by_pk.values():
			for key, value in edits[track.track_file_path].items():
				if value.strip() == track.tags.get(key, ""):
					continue  # only what was changed is checked; existing gaps never block a save
				message = tag_editor.validate(key, value)
				if message:
					errors[f"t{track.pk}-{key}"] = message
		if not errors:
			try:
				result = tag_editor.save(edits, reimport_paths=[track.track_file_path for track in readable])
			except tag_editor.TagEditError as error:
				messages.error(request, str(error))
			else:
				_save_message(request, result, "album")
				if Album.objects.filter(pk=album.pk).exists():
					return redirect("library:album-detail", album_id=album.pk)
				new_album = Track.objects.filter(pk__in=result.track_ids).values_list("album_id", flat=True).first()
				return redirect("library:album-detail", album_id=new_album) if new_album else redirect("library:album-list")
		else:
			messages.error(request, f"{len(errors)} value{pluralize(len(errors))} need{'' if len(errors) != 1 else 's'} fixing before saving.")

	# Offered when a tag is empty: what the folder layout says, else what was found since (resolver).
	from_folder = derived_tags(readable[0].track_file_path) if readable else {}
	from_folder["albumartist"] = from_folder.get("artist", "")
	found = _found_album_values(album)
	header = []
	for key, label in tag_editor.ALBUM_FIELDS:
		value, mixed = _shared_value(track.tags.get(key, "") for track in readable)
		suggested, origin = "", ""
		if not value and not mixed:
			if from_folder.get(key):
				suggested, origin = from_folder[key], "Folder"
			elif found.get(key):
				suggested, origin = found[key]
		header.append({
			"key": key, "label": label, "initial": value, "mixed": mixed, "suggested": suggested, "origin": origin,
			"value": posted.get(f"h-{key}", value) if posted else value,
			"in_table": key not in tag_editor.ALBUM_ONLY,
		})
	rows = []
	for track in tracks:
		cells = []
		for key, label in tag_editor.TRACK_FIELDS:
			initial = track.tags.get(key, "")
			name = f"t{track.pk}-{key}"
			cells.append({
				"key": key, "label": label, "name": name, "initial": initial,
				"value": posted.get(name, initial) if posted else initial, "error": errors.get(name),
				# a track number the file name carries, offered while the tag is empty
				"suggested": str(track.resolved_number) if key == "tracknumber" and not initial and track.resolved_number
				and track.resolved_sources.get("number") == "filename" else "",
			})
		rows.append({"track": track, "cells": cells, "file_name": Path(track.track_file_path).name if track.track_file_path else ""})

	return render(request, "library/album_edit_tags.html", {
		"album": album,
		"header": header,
		"columns": tag_editor.TRACK_FIELDS,
		"rows": rows,
		"readable_count": len(readable),
		"missing_count": len(tracks) - len(readable),
		"edit_token": _edit_token("album", album.pk, readable),
		"has_suggestions": any(field["suggested"] for field in header) or any(cell["suggested"] for row in rows for cell in row["cells"]),
		"importing": is_importing(),
	}, status=400 if errors else 200)


@login_required
def artist_edit_tags(request, artist_id):
	"""Edit Tags for one artist: name, country and formed year, written to all of the artist's files."""
	artist = get_object_or_404(Artist, pk=artist_id)
	tracks = _readable_tracks(Track.objects.filter(album__artist=artist))
	readable = [track for track in tracks if track.readable]
	errors, posted = {}, None

	if request.method == "POST":
		try:
			files = _check_edit_token(request, "artist", artist.pk)
		except ValueError as error:
			messages.error(request, str(error))
			return redirect("library:artist-edit-tags", artist_id=artist.pk)
		posted = request.POST
		targets = [track for track in readable if track.pk in files]
		edits = {track.track_file_path: {} for track in targets}
		for key, _ in tag_editor.ARTIST_FIELDS:
			value, initial = posted.get(f"h-{key}", ""), posted.get(f"h-{key}-initial", "")
			if value.strip() == initial.strip():
				continue
			message = tag_editor.validate(key, value)
			if message:
				errors[f"h-{key}"] = message
				continue
			for track in targets:
				edits[track.track_file_path][key] = value
				# An album artist that named this artist follows the new name.
				if key == "artist" and track.tags.get("albumartist", "").casefold() == track.tags.get("artist", "").casefold() != "":
					edits[track.track_file_path]["albumartist"] = value
		if not errors:
			try:
				result = tag_editor.save({path: values for path, values in edits.items() if values},
				                         reimport_paths=[track.track_file_path for track in readable])
			except tag_editor.TagEditError as error:
				messages.error(request, str(error))
			else:
				_save_message(request, result, "artist")
				if Artist.objects.filter(pk=artist.pk).exists():
					return redirect("library:artist-detail", artist_id=artist.pk)
				new_artist = Track.objects.filter(pk__in=result.track_ids).values_list("album__artist_id", flat=True).first()
				return redirect("library:artist-detail", artist_id=new_artist) if new_artist else redirect("library:artist-list")
		else:
			messages.error(request, f"{len(errors)} value{pluralize(len(errors))} need{'' if len(errors) != 1 else 's'} fixing before saving.")

	fields = []
	found_artist = {}
	for key, found_value, source in (
		("artist_country", artist.resolved_country, artist.resolved_sources.get("country")),
		("artist_formed_year", artist.resolved_formed_year, artist.resolved_sources.get("formed_year")),
	):
		if found_value and source and source != "tags":
			found_artist[key] = (str(found_value), resolver_api.source_label(source))
	for key, label in tag_editor.ARTIST_FIELDS:
		value, mixed = _shared_value(track.tags.get(key, "") for track in readable)
		suggested, origin = found_artist.get(key, ("", "")) if not value and not mixed else ("", "")
		fields.append({
			"key": key, "label": label, "initial": value, "mixed": mixed, "suggested": suggested, "origin": origin,
			"value": posted.get(f"h-{key}", value) if posted else value, "error": errors.get(f"h-{key}"),
		})
	albums = queries.albums(tag_year_only=True).filter(artist_id=artist.pk).order_by("album_release_year", Lower("album_name"))
	return render(request, "library/artist_edit_tags.html", {
		"artist": artist,
		"fields": fields,
		"albums": albums,
		"readable_count": len(readable),
		"missing_count": len(tracks) - len(readable),
		"edit_token": _edit_token("artist", artist.pk, readable),
		"importing": is_importing(),
	}, status=400 if errors else 200)
