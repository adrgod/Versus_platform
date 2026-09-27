import json
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from datetime import date

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST
from mutagen.easyid3 import EasyID3

from db import db_operations
from sources import local_files

from . import repository
from .scan_service import start_scan


@login_required
def dashboard(request):
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		state = repository.get_library_state(connection)
		stats = repository.dashboard_stats(connection)
		recent_albums = connection.execute(
			"""
			SELECT al.album_id, al.album_name, al.release_year, al.genre, al.album_cover,
				   ar.artist_name, COUNT(t.track_id) AS track_count
			FROM tbl_album al
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			LEFT JOIN tbl_track t ON t.album_id = al.album_id
			GROUP BY al.album_id
			ORDER BY al.when_loaded DESC, al.album_name COLLATE NOCASE
			LIMIT 6
			"""
		).fetchall()
	finally:
		connection.close()

	albums = [dict(album) for album in recent_albums]
	for album in albums:
		album["cover_uri"] = repository.album_cover_data_uri(album["album_cover"])
	return render(request, "library/dashboard.html", {
		"state": state,
		"stats": stats,
		"recent_albums": albums,
	})


@login_required
@require_GET
def folder_browser(request):
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		state = repository.get_library_state(connection)
		selected_folder = Path(state["source_folder"]).expanduser().resolve()
	finally:
		connection.close()

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
		connection = repository.connect()
		try:
			repository.ensure_schema(connection)
			repository.get_library_state(connection)
			connection.execute(
				"UPDATE tbl_library_state SET source_folder = ? WHERE singleton_id = 1",
				(str(selected),),
			)
			connection.commit()
		finally:
			connection.close()
		messages.success(request, f"Library location set to {selected}")
	except (OSError, RuntimeError, ValueError) as error:
		messages.error(request, str(error))
		return redirect("library:folder-browser")
	return redirect("library:dashboard")


@login_required
@require_POST
def refresh_library(request):
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		state = repository.get_library_state(connection)
		source_folder = state["source_folder"]
	finally:
		connection.close()

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


@login_required
def genre_index(request):
	search = request.GET.get("q", "").strip()
	pattern = f"%{search}%"
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		rows = connection.execute(
			"""
			SELECT al.*, ar.artist_name, COUNT(t.track_id) AS track_count
			FROM tbl_album al
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			LEFT JOIN tbl_track t ON t.album_id = al.album_id
			GROUP BY al.album_id
			ORDER BY al.genre COLLATE NOCASE, ar.artist_name COLLATE NOCASE,
			         al.album_name COLLATE NOCASE
			""",
		).fetchall()
	finally:
		connection.close()
	genres_by_key = {}
	for row in rows:
		for name in _split_genres(row["genre"]):
			key = name.casefold()
			if search and search.casefold() not in name.casefold():
				continue
			genre = genres_by_key.setdefault(key, {
				"name": name,
				"album_ids": set(),
				"artist_ids": set(),
				"track_count": 0,
			})
			genre["album_ids"].add(row["album_id"])
			genre["artist_ids"].add(row["artist_id"])
			genre["track_count"] += row["track_count"]
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
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		loaded_dates = connection.execute(
			"""
			SELECT DATE(t.when_loaded) AS loaded_date,
			       COUNT(t.track_id) AS track_count,
			       COUNT(DISTINCT al.album_id) AS album_count,
			       COUNT(DISTINCT ar.artist_id) AS artist_count
			FROM tbl_track t
			JOIN tbl_album al ON al.album_id = t.album_id
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			WHERE t.when_loaded IS NOT NULL
			GROUP BY DATE(t.when_loaded)
			ORDER BY loaded_date DESC
			"""
		).fetchall()
	finally:
		connection.close()
	loaded_dates = [dict(row) for row in loaded_dates]
	for loaded in loaded_dates:
		loaded["day"] = date.fromisoformat(loaded["loaded_date"])
	return render(request, "library/loaded_date_index.html", {"loaded_dates": loaded_dates})


def _split_genres(value):
	return [part.strip() for part in re.split(r"[,;|]+", value or "") if part.strip()]


@login_required
def genre_detail(request):
	requested_genre = request.GET.get("name", "").strip()
	if not requested_genre:
		return redirect("library:browse")
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		all_albums = connection.execute(
			"""
			SELECT al.*, ar.artist_name, COUNT(t.track_id) AS track_count
			FROM tbl_album al
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			LEFT JOIN tbl_track t ON t.album_id = al.album_id
			GROUP BY al.album_id
			ORDER BY ar.artist_name COLLATE NOCASE, al.release_year, al.album_name COLLATE NOCASE
			"""
		).fetchall()
		matching_albums = [
			row for row in all_albums
			if any(name.casefold() == requested_genre.casefold() for name in _split_genres(row["genre"]))
		]
		if not matching_albums:
			from django.http import Http404
			raise Http404("Genre not found")
		genre_name = next(
			name for name in _split_genres(matching_albums[0]["genre"])
			if name.casefold() == requested_genre.casefold()
		)
		album_ids = [row["album_id"] for row in matching_albums]
		placeholders = ",".join("?" for _ in album_ids)
		tracks = connection.execute(
			f"""
			SELECT t.*, al.album_name, al.album_id, ar.artist_id, ar.artist_name
			FROM tbl_track t
			JOIN tbl_album al ON al.album_id = t.album_id
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			WHERE t.album_id IN ({placeholders})
			ORDER BY ar.artist_name COLLATE NOCASE, al.album_name COLLATE NOCASE,
			         CAST(t.track_number AS INTEGER), t.track_name COLLATE NOCASE
			""",
			album_ids,
		).fetchall()
	finally:
		connection.close()

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
		row["artist_id"]: {"artist_id": row["artist_id"], "artist_name": row["artist_name"]}
		for row in matching_albums
	}
	track_rows = []
	for track in tracks:
		track_row = dict(track)
		track_row["player_url"] = None
		if not track["file_path"] or not repository.is_allowed_file(track["file_path"]):
			track_rows.append(track_row)
			continue
		player_track = {
			"url": reverse("library:stream-track", args=(track["track_id"],)),
			"title": track["track_name"],
			"album": track["album_name"],
			"artist": track["artist_name"],
		}
		track_row["player_url"] = player_track["url"]
		track_rows.append(track_row)
		tracks_by_album.setdefault(track["album_id"], []).append(player_track)
		tracks_by_artist.setdefault(track["artist_id"], []).append(player_track)

	albums = [dict(row) for row in matching_albums]
	for album in albums:
		album["cover_uri"] = repository.album_cover_data_uri(album["album_cover"])
		album["queue_tracks"] = tracks_by_album.get(album["album_id"], [])
		album["queue_id"] = f"album-queue-{album['album_id']}"
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
		from django.http import Http404
		raise Http404("Loaded date not found")

	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		date_row = connection.execute(
			"SELECT DATE(when_loaded) AS loaded_date FROM tbl_track WHERE DATE(when_loaded) = ? LIMIT 1",
			(loaded_date.isoformat(),),
		).fetchone()
		if date_row is None:
			from django.http import Http404
			raise Http404("Loaded date not found")
		matching_albums = connection.execute(
			"""
			SELECT DISTINCT al.*, ar.artist_name, COUNT(t.track_id) AS track_count
			FROM tbl_album al
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			JOIN tbl_track t ON t.album_id = al.album_id
			WHERE DATE(t.when_loaded) = ?
			GROUP BY al.album_id
			ORDER BY ar.artist_name COLLATE NOCASE, al.album_name COLLATE NOCASE
			""",
			(loaded_date.isoformat(),),
		).fetchall()
		tracks = connection.execute(
			"""
			SELECT t.*, al.album_name, al.album_id, ar.artist_id, ar.artist_name
			FROM tbl_track t
			JOIN tbl_album al ON al.album_id = t.album_id
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			WHERE DATE(t.when_loaded) = ?
			ORDER BY ar.artist_name COLLATE NOCASE, al.album_name COLLATE NOCASE,
			         CAST(t.track_number AS INTEGER), t.track_name COLLATE NOCASE
			""",
			(loaded_date.isoformat(),),
		).fetchall()
	finally:
		connection.close()

	artists, albums, track_rows = _prepare_browse_detail(matching_albums, tracks)
	return render(request, "library/loaded_date_detail.html", {
		"loaded_date": loaded_date,
		"artists": artists,
		"albums": albums,
		"tracks": track_rows,
	})


@login_required
def artist_list(request):
	search = request.GET.get("q", "").strip()
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		rows = connection.execute(
			"""
			SELECT ar.artist_id, ar.artist_name, COUNT(DISTINCT al.album_id) AS album_count,
				   COUNT(t.track_id) AS track_count
			FROM tbl_artist ar
			LEFT JOIN tbl_album al ON al.artist_id = ar.artist_id
			LEFT JOIN tbl_track t ON t.album_id = al.album_id
			WHERE ar.artist_name LIKE ? COLLATE NOCASE
			GROUP BY ar.artist_id
			ORDER BY ar.artist_name COLLATE NOCASE
			LIMIT 500
			""",
			(f"%{search}%",),
		).fetchall()
	finally:
		connection.close()
	return render(request, "library/artist_list.html", {"artists": rows, "search": search})


@login_required
def artist_detail(request, artist_id):
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		artist = connection.execute(
			"SELECT * FROM tbl_artist WHERE artist_id = ?", (artist_id,)
		).fetchone()
		if artist is None:
			from django.http import Http404
			raise Http404("Artist not found")
		rows = connection.execute(
			"""
			SELECT al.*, COUNT(t.track_id) AS track_count
			FROM tbl_album al LEFT JOIN tbl_track t ON t.album_id = al.album_id
			WHERE al.artist_id = ?
			GROUP BY al.album_id
			ORDER BY al.release_year, al.album_name COLLATE NOCASE
			""",
			(artist_id,),
		).fetchall()
	finally:
		connection.close()
	albums = [dict(row) for row in rows]
	for album in albums:
		album["cover_uri"] = repository.album_cover_data_uri(album["album_cover"])
	return render(request, "library/artist_detail.html", {"artist": artist, "albums": albums})


@login_required
def album_list(request):
	search = request.GET.get("q", "").strip()
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		rows = connection.execute(
			"""
			SELECT al.*, ar.artist_name, COUNT(t.track_id) AS track_count
			FROM tbl_album al
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			LEFT JOIN tbl_track t ON t.album_id = al.album_id
			WHERE al.album_name LIKE ? COLLATE NOCASE OR ar.artist_name LIKE ? COLLATE NOCASE
			GROUP BY al.album_id
			ORDER BY ar.artist_name COLLATE NOCASE, al.release_year, al.album_name COLLATE NOCASE
			LIMIT 500
			""",
			(f"%{search}%", f"%{search}%"),
		).fetchall()
	finally:
		connection.close()
	albums = [dict(row) for row in rows]
	for album in albums:
		album["cover_uri"] = repository.album_cover_data_uri(album["album_cover"])
	return render(request, "library/album_list.html", {"albums": albums, "search": search})


@login_required
def album_detail(request, album_id):
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		album = connection.execute(
			"""
			SELECT al.*, ar.artist_name
			FROM tbl_album al JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			WHERE al.album_id = ?
			""",
			(album_id,),
		).fetchone()
		if album is None:
			from django.http import Http404
			raise Http404("Album not found")
		tracks = connection.execute(
			"SELECT * FROM tbl_track WHERE album_id = ? ORDER BY CAST(track_number AS INTEGER), track_number, track_name COLLATE NOCASE",
			(album_id,),
		).fetchall()
	finally:
		connection.close()
	album = dict(album)
	album["cover_uri"] = repository.album_cover_data_uri(album["album_cover"])
	playable_tracks = [
		{
			"url": reverse("library:stream-track", args=(track["track_id"],)),
			"title": track["track_name"],
			"album": album["album_name"],
		}
		for track in tracks
		if track["file_path"] and repository.is_allowed_file(track["file_path"])
	]
	return render(request, "library/album_detail.html", {
		"album": album,
		"tracks": tracks,
		"player_tracks": playable_tracks,
	})


@login_required
@require_GET
def stream_track(request, track_id):
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		track = connection.execute(
			"SELECT file_path, track_name FROM tbl_track WHERE track_id = ?", (track_id,)
		).fetchone()
	finally:
		connection.close()
	if track is None or not track["file_path"] or not repository.is_allowed_file(track["file_path"]):
		return HttpResponse("Audio file is unavailable.", status=404)

	path = Path(track["file_path"]).resolve()
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


def _quality_rows(connection, search=""):
	rows = connection.execute(
		"""
		SELECT t.track_id, t.track_name, t.track_number, t.dq_confidence, t.file_path,
			   al.album_name, al.release_year, al.genre, ar.artist_name
		FROM tbl_track t
		JOIN tbl_album al ON al.album_id = t.album_id
		JOIN tbl_artist ar ON ar.artist_id = al.artist_id
		WHERE t.track_name LIKE ? COLLATE NOCASE
		   OR al.album_name LIKE ? COLLATE NOCASE
		   OR ar.artist_name LIKE ? COLLATE NOCASE
		ORDER BY ar.artist_name COLLATE NOCASE, al.album_name COLLATE NOCASE,
				 CAST(t.track_number AS INTEGER), t.track_name COLLATE NOCASE
		LIMIT 1000
		""",
		(f"%{search}%", f"%{search}%", f"%{search}%"),
	).fetchall()
	config = repository.get_config()
	fields = config["quality_attributes"]
	field_values = {
		"artist": "artist_name",
		"album": "album_name",
		"title": "track_name",
		"tracknumber": "track_number",
		"date": "release_year",
		"genre": "genre",
	}
	result = []
	for row in rows:
		missing = [
			field for field in fields
			if not row[field_values.get(field, field)]
		]
		item = dict(row)
		item["missing_fields"] = missing
		item["quality_percent"] = round(100 * (len(fields) - len(missing)) / len(fields)) if fields else 100
		result.append(item)
	return result


@login_required
def quality_report(request):
	search = request.GET.get("q", "").strip()
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		tracks = _quality_rows(connection, search)
	finally:
		connection.close()
	return render(request, "library/quality_report.html", {
		"tracks": tracks,
		"search": search,
		"incomplete_count": sum(bool(track["missing_fields"]) for track in tracks),
		"average_score": round(sum(track["quality_percent"] for track in tracks) / len(tracks)) if tracks else 0,
	})


def _tag_fields():
	config = repository.get_config()
	return [field for field in config["file_attributes"] if field != "length"]


def _easyid3_values(file_path, fields):
	tags = EasyID3(file_path)
	return {field: ", ".join(tags.get(field, [])) for field in fields}


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
	connection = repository.connect()
	try:
		repository.ensure_schema(connection)
		track = connection.execute(
			"""
			SELECT t.*, al.album_name, ar.artist_name
			FROM tbl_track t
			JOIN tbl_album al ON al.album_id = t.album_id
			JOIN tbl_artist ar ON ar.artist_id = al.artist_id
			WHERE t.track_id = ?
			""",
			(track_id,),
		).fetchone()
	finally:
		connection.close()
	if track is None:
		from django.http import Http404
		raise Http404("Track not found")
	if not track["file_path"] or not repository.is_allowed_file(track["file_path"]):
		raise ValueError("This track has no usable path. Refresh the library before editing tags.")
	return track


@login_required
def edit_track_tags(request, track_id):
	try:
		track = _get_track_for_edit(track_id)
	except ValueError as error:
		messages.error(request, str(error))
		return redirect("library:quality-report")
	path = Path(track["file_path"]).resolve()
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
		path = Path(track["file_path"]).resolve()
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

	connection = repository.connect()
	try:
		old = connection.execute("SELECT album_id FROM tbl_track WHERE track_id = ?", (track_id,)).fetchone()
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

		with (repository.PROJECT_ROOT / "config.json").open(encoding="utf-8") as config_file:
			config = json.load(config_file)
		record = local_files.read_metadata(path, config)
		record["album_cover"] = local_files.read_artwork(path)
		record["dq_confidence"] = local_files.calculate_confidence_for_track(record, record["file_path"])

		connection.execute("BEGIN IMMEDIATE")
		connection.execute("DELETE FROM tbl_track WHERE track_id = ?", (track_id,))
		db_operations.db_create_schema(connection)
		db_operations.db_load_data(connection, [record])
		if old is not None:
			connection.execute(
				"DELETE FROM tbl_album WHERE album_id = ? AND NOT EXISTS (SELECT 1 FROM tbl_track WHERE album_id = ?)",
				(old["album_id"], old["album_id"]),
			)
		connection.execute(
			"DELETE FROM tbl_artist WHERE NOT EXISTS (SELECT 1 FROM tbl_album WHERE artist_id = tbl_artist.artist_id)"
		)
		connection.commit()
	except Exception as error:
		connection.rollback()
		connection.close()
		shutil.copy2(backup_path, path)
		messages.error(request, f"Could not save tags; original file restored from backup: {error}")
		return redirect("library:edit-tags", track_id=track_id)
	else:
		connection.close()

	messages.success(request, f"Tags saved and verified. Backup: {backup_path.name}")
	return redirect("library:album-list")
