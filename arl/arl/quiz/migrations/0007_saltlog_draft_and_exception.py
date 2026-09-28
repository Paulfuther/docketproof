import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("quiz", "0006_checklist_followup_help_text"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Existing rows were finished logs. Add them as submitted, then
        # change only the default so new rows start as drafts.
        migrations.AddField(
            model_name="saltlog",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("submitted", "Submitted"),
                    ("completed", "Completed (PDF generated)"),
                ],
                db_index=True,
                default="submitted",
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name="saltlog",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("submitted", "Submitted"),
                    ("completed", "Completed (PDF generated)"),
                ],
                db_index=True,
                default="draft",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="submitted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="submitted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="submitted_salt_logs",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="updated_at",
            field=models.DateTimeField(auto_now=True),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="levels_ok",
            field=models.BooleanField(
                blank=True,
                help_text=(
                    "True when salt levels were acceptable. "
                    "False opens the exception path (what, who, when)."
                ),
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="exception_what",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="exception_who",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="exception_when",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="saltlog",
            name="pdf_path",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Dropbox path of the submitted PDF. "
                    "Older PDFs stay where they were."
                ),
                max_length=500,
            ),
        ),
        migrations.AlterField(
            model_name="saltlog",
            name="area_salted",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="saltlog",
            name="date_salted",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="saltlog",
            name="image_folder",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AlterField(
            model_name="saltlog",
            name="store",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="user.store",
            ),
        ),
        migrations.AlterField(
            model_name="saltlog",
            name="time_salted",
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AlterModelOptions(
            name="saltlog",
            options={"ordering": ["-hidden_timestamp"]},
        ),
    ]
