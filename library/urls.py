from django.urls import path

from . import views


app_name = "library"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("browse/", views.browse, name="browse"),
    path("browse/genres/", views.genre_index, name="genre-index"),
    path("browse/loaded-dates/", views.loaded_date_index, name="loaded-date-index"),
    path("explore/", views.explore, name="explore"),
    path("explore/decades/", views.decade_index, name="decade-index"),
    path("enrichment/", views.enrichment_status, name="enrichment"),
    path("enrichment/start/", views.enrichment_start, name="enrichment-start"),
    path("enrichment/stop/", views.enrichment_stop, name="enrichment-stop"),
    path("enrichment/notes/", views.notes_start, name="notes-start"),
    path("enrichment/<str:kind>/<int:object_id>/id/", views.set_external_id, name="set-external-id"),
    path("genre/", views.genre_detail, name="genre-detail"),
    path("loaded-date/", views.loaded_date_detail, name="loaded-date-detail"),
    path("location/", views.folder_browser, name="folder-browser"),
    path("location/choose/", views.choose_folder, name="choose-folder"),
    path("refresh/", views.refresh_library, name="refresh"),
    path("tags/read/", views.read_local_tags_view, name="read-local-tags"),
    path("artists/", views.artist_list, name="artist-list"),
    path("artists/<int:artist_id>/", views.artist_detail, name="artist-detail"),
    path("albums/", views.album_list, name="album-list"),
    path("albums/<int:album_id>/", views.album_detail, name="album-detail"),
    path("tracks/<int:track_id>/audio/", views.stream_track, name="stream-track"),
    path("batches/", views.batch_index, name="batch-index"),
    path("batches/<str:day>/", views.batch_detail, name="batch-detail"),
    path("batches/<str:day>/preview/", views.batch_preview, name="batch-preview"),
    path("batches/<str:day>/write/", views.batch_write, name="batch-write"),
    path("batches/<str:day>/status/", views.batch_status, name="batch-status"),
    path("quality/", views.quality_report, name="quality-report"),
    path("quality/completeness/", views.completeness_report, name="completeness-report"),
    path("tracks/<int:track_id>/edit/", views.edit_track_tags, name="edit-tags"),
    path("albums/<int:album_id>/edit/", views.album_edit_tags, name="album-edit-tags"),
    path("artists/<int:artist_id>/edit/", views.artist_edit_tags, name="artist-edit-tags"),
    path("tracks/<int:track_id>/confirm/", views.confirm_tag_edit, name="confirm-tag-edit"),
]