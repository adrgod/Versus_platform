import mimetypes
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from typing import cast

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import Count, F, Q
from django.db.models.functions import Lower, TruncDate
from django.http import Http404, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from mutagen.easyid3 import EasyID3

from . import queries, repository
from .models import Artist, LibraryState, Track
from .scan_service import start_scan
from .services.files_scanner import (
	calculate_confidence_for_track,
	read_artwork_from_id3,
	read_file_metadata,
)
from .services.importer import import_records, prune_empty


def _with_covers(albums):
	albums = list(albums)
	for album in albums:
		album.cover_uri = repository.album_cover_data_uri(album.album_cover)
	return albums


@login_required
def dashboard(request):
	recent_albums = queries.albums().order_by("-when_loaded", Lower("album_name"))[:6]
	return render(request, "library/dashboard.html", {
		"state": LibraryState.load(),
		"stats": queries.dashboard_stats(),
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
		"roots": roots,
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
			messages.success(request, "Library scan started. This page will show the result when it finishes.")
		else:
			messages.warning(request, "A library scan is already running.")
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
			"name": "Loaded date",
			"description": "Browse music by the date it entered the database.",
			"url": reverse("library:loaded-date-index"),
			"kind": "loaded-date",
		},
	]
	return render(request, "library/browse.html", {"data_elements": data_elements})


def _split_genres(value):
	return [part.strip() for part in re.split(r"[,;|]+", value or "") if part.strip()]


@login_required
def genre_index(request):
	search = request.GET.get("q", "").strip()
	rows = queries.albums().order_by(
		Lower("album_genre"), Lower("artist__artist_name"), Lower("album_name")
	)
	genres_by_key = {}
	for row in rows:
		for name in _split_genres(row.genre):
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
		if any(name.casefold() == requested_genre.casefold() for name in _split_genres(album.genre))
	]
	if not matching_albums:
		raise Http404("Genre not found")
	genre_name = next(
		name for name in _split_genres(matching_albums[0].genre)
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
	rows = (
		queries.artists()
		.filter(artist_name__icontains=search)
		.order_by(Lower("artist_name"))[:500]
	)
	return render(request, "library/artist_list.html", {"artists": rows, "search": search})


@login_required
def artist_detail(request, artist_id):
	artist = get_object_or_404(Artist, pk=artist_id)
	albums = (
		queries.albums()
		.filter(artist_id=artist_id)
		.order_by("album_release_year", Lower("album_name"))
	)
	return render(request, "library/artist_detail.html", {
		"artist": artist,
		"albums": _with_covers(albums),
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


@login_required
def album_detail(request, album_id):
	album = get_object_or_404(queries.albums(), pk=album_id)
	album.cover_uri = repository.album_cover_data_uri(album.album_cover)
	tracks = list(
		queries.tracks().filter(album_id=album_id).order_by("track_number", Lower("track_title"))
	)
	playable_tracks = [
		{
			"url": reverse("library:stream-track", args=(track.track_id,)),
			"title": track.track_name,
			"album": album.album_name,
		}
		for track in tracks
		if track.file_path and repository.is_allowed_file(track.file_path)
	]
	return render(request, "library/album_detail.html", {
		"album": album,
		"tracks": tracks,
		"player_tracks": playable_tracks,
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
		.annotate(release_year=F("album__album_release_year"), genre=F("album__album_genre"))
		.filter(
			Q(track_title__icontains=search)
			| Q(album__album_name__icontains=search)
			| Q(album__artist__artist_name__icontains=search)
		)
		.order_by(*queries.track_order())[:1000]
	)
	fields = repository.get_config()["quality_attributes"]
	row_attributes = {
		"artist": "artist_name",
		"album": "album_name",
		"title": "track_name",
		"tracknumber": "track_number",
		"date": "release_year",
		"genre": "genre",
	}
	result = []
	for row in rows:
		row.missing_fields = [
			field for field in fields
			if not getattr(row, row_attributes.get(field, field))
		]
		row.quality_percent = (
			round(100 * (len(fields) - len(row.missing_fields)) / len(fields)) if fields else 100
		)
		result.append(row)
	return result


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
	backup_folder = repository.get_backup_directory()
	backup_folder.mkdir(parents=True, exist_ok=True)
	backup_path = backup_folder / f"track-{track_id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.mp3"
	shutil.copy2(path, backup_path)

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

		# The tags may change which artist/album/track this is, so the old row is
		# replaced and anything left empty is pruned. If the new tags cannot be
		# stored the whole change rolls back and the file is restored below.
		with transaction.atomic():
			Track.objects.filter(pk=track_id).delete()
			stats = import_records([record])
			if stats["skipped"]:
				raise ValueError("Artist, album and title cannot be empty; the original file will be restored.")
			prune_empty()
	except Exception as error:
		shutil.copy2(backup_path, path)
		messages.error(request, f"Could not save tags; original file restored from backup: {error}")
		return redirect("library:edit-tags", track_id=track_id)

	messages.success(request, f"Tags saved and verified. Backup: {backup_path.name}")
	return redirect("library:album-list")
