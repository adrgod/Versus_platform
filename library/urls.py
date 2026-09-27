from django.urls import path

from . import views


app_name = "library"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("browse/", views.browse, name="browse"),
    path("browse/genres/", views.genre_index, name="genre-index"),
    path("browse/loaded-dates/", views.loaded_date_index, name="loaded-date-index"),
    path("genre/", views.genre_detail, name="genre-detail"),
    path("loaded-date/", views.loaded_date_detail, name="loaded-date-detail"),
    path("location/", views.folder_browser, name="folder-browser"),
    path("location/choose/", views.choose_folder, name="choose-folder"),
    path("refresh/", views.refresh_library, name="refresh"),
    path("artists/", views.artist_list, name="artist-list"),
    path("artists/<int:artist_id>/", views.artist_detail, name="artist-detail"),
    path("albums/", views.album_list, name="album-list"),
    path("albums/<int:album_id>/", views.album_detail, name="album-detail"),
    path("tracks/<int:track_id>/audio/", views.stream_track, name="stream-track"),
    path("quality/", views.quality_report, name="quality-report"),
    path("tracks/<int:track_id>/edit/", views.edit_track_tags, name="edit-tags"),
    path("tracks/<int:track_id>/confirm/", views.confirm_tag_edit, name="confirm-tag-edit"),
]