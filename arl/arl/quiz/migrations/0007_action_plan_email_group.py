from django.db import migrations

ACTION_PLAN_EMAIL_GROUP = "action_plan_email"


def create_action_plan_email_group(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.get_or_create(name=ACTION_PLAN_EMAIL_GROUP)


class Migration(migrations.Migration):
    dependencies = [
        ("quiz", "0006_checklist_followup_help_text"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(
            create_action_plan_email_group,
            migrations.RunPython.noop,
        ),
    ]
