import arl.quiz.models
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("quiz", "0004_checklist_store"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklisttemplate",
            name="document_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="External document id, e.g. ENMCDS840-6.3",
                max_length=80,
            ),
        ),
        migrations.AddField(
            model_name="checklisttemplate",
            name="parent_sop",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="checklisttemplate",
            name="purpose",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="checklisttemplate",
            name="instructions",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="item_code",
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="section",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="response_type",
            field=models.CharField(
                choices=[
                    ("yes_no_na", "Yes / No / N/A"),
                    ("text", "Text"),
                    ("date", "Date"),
                ],
                default="yes_no_na",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="required",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="allow_photo",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="responsibility_assignable",
            field=models.BooleanField(
                default=True,
                help_text="Show L/S responsibility when the answer is Y or N.",
            ),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="create_action_on",
            field=models.JSONField(
                blank=True,
                default=arl.quiz.models.default_create_action_on,
                help_text='Answer codes that open a 6.4 action plan, e.g. ["N"].',
            ),
        ),
        migrations.AddField(
            model_name="checklisttemplateitem",
            name="action_plan_form",
            field=models.CharField(blank=True, max_length=80),
        ),
        migrations.AddField(
            model_name="checklistitem",
            name="section",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="checklistitem",
            name="responsibility",
            field=models.CharField(
                blank=True,
                choices=[
                    ("L", "L — Associate / direct employer / franchisee"),
                    ("S", "S — Supplier / owner / franchisor / gas company"),
                ],
                default="",
                max_length=1,
            ),
        ),
        migrations.AddField(
            model_name="checklistitem",
            name="text_value",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="checklistitem",
            name="result",
            field=models.CharField(
                blank=True,
                choices=[("yes", "Y"), ("no", "N"), ("na", "N/A")],
                default="",
                max_length=5,
            ),
        ),
        migrations.CreateModel(
            name="ChecklistActionItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("action_item", models.CharField(max_length=500)),
                ("action_required", models.TextField(blank=True)),
                ("who", models.CharField(blank=True, max_length=200)),
                ("target_date", models.DateField(blank=True, null=True)),
                ("completion_date", models.DateField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Open"), ("done", "Done")],
                        default="open",
                        max_length=10,
                    ),
                ),
                ("note", models.TextField(blank=True)),
                (
                    "photo",
                    models.ImageField(
                        blank=True,
                        max_length=512,
                        null=True,
                        upload_to=arl.quiz.models.action_photo_upload_to,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "checklist",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_items",
                        to="quiz.checklist",
                    ),
                ),
                (
                    "checklist_item",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="action_item",
                        to="quiz.checklistitem",
                    ),
                ),
            ],
            options={
                "ordering": ["checklist_item__order", "id"],
            },
        ),
    ]
