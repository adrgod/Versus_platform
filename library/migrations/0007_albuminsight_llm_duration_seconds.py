from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0006_album_notes_lyrics_insights"),
    ]

    operations = [
        migrations.AddField(
            model_name="albuminsight",
            name="llm_duration_seconds",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True),
        ),
    ]
