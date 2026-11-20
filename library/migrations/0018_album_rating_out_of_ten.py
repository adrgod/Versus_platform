from django.db import migrations


def to_ten(apps, schema_editor):
    """Album ratings were 1-5 stars; they are now 1-10, the same score as the album's review."""
    AlbumRating = apps.get_model("library", "AlbumRating")
    Review = apps.get_model("library", "Review")
    for rating in AlbumRating.objects.all():
        rating.stars = min(10, rating.stars * 2)
        rating.save(update_fields=["stars"])
    for review in Review.objects.all():  # a review's score is the album's rating too
        AlbumRating.objects.update_or_create(user_id=review.user_id, album_id=review.album_id,
                                             defaults={"stars": review.score})


def to_five(apps, schema_editor):
    AlbumRating = apps.get_model("library", "AlbumRating")
    for rating in AlbumRating.objects.all():
        rating.stars = max(1, min(5, round(rating.stars / 2)))
        rating.save(update_fields=["stars"])


class Migration(migrations.Migration):
    dependencies = [("library", "0017_document_title_kind")]
    operations = [migrations.RunPython(to_ten, to_five)]
