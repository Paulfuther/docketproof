from django.db import migrations, models
from django.utils.text import slugify

SPLIT_NAME_MARKERS = (
    "security assessment",
    "workplace inspection",
    "site security",
    "workplace security",
)
SPLIT_DOCUMENT_IDS = ("enmcds840-6.3", "enmccl007")


def _slug_key(value) -> str:
    return slugify(str(value or "").replace("_", " "))


def _template_should_split(name, document_id) -> bool:
    folded = (name or "").casefold()
    slugged = _slug_key(name)
    for marker in SPLIT_NAME_MARKERS:
        if marker in folded or _slug_key(marker) in slugged:
            return True
    doc = (document_id or "").strip().casefold()
    if doc in SPLIT_DOCUMENT_IDS:
        return True
    doc_slug = _slug_key(document_id)
    return bool(doc_slug) and doc_slug in {
        _slug_key(item) for item in SPLIT_DOCUMENT_IDS
    }


def enable_split_action_plan_delivery(apps, schema_editor):
    ChecklistTemplate = apps.get_model("quiz", "ChecklistTemplate")
    for template in ChecklistTemplate.objects.all().iterator():
        if _template_should_split(template.name, template.document_id):
            template.split_action_plan_delivery = True
            template.save(update_fields=["split_action_plan_delivery"])


class Migration(migrations.Migration):
    dependencies = [
        ("quiz", "0007_action_plan_email_group"),
    ]

    operations = [
        migrations.AddField(
            model_name="checklisttemplate",
            name="split_action_plan_delivery",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Send the 6.4 action plan as its own PDF and email. "
                    "Security Assessment and Workplace Inspection (BC & ON) are on. "
                    "Leave this off and a matching name, document id, or 6.4 form "
                    "still splits."
                ),
            ),
        ),
        migrations.RunPython(
            enable_split_action_plan_delivery,
            migrations.RunPython.noop,
        ),
    ]
