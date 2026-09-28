from django.db import migrations


class Migration(migrations.Migration):
    """Join salt-log #458 and the action-plan delivery branch.

    Both added a migration that depends on quiz 0006, so Django needs one
    leaf before migrate can run.
    """

    dependencies = [
        ("quiz", "0007_saltlog_draft_and_exception"),
        ("quiz", "0008_checklisttemplate_split_action_plan_delivery"),
    ]

    operations = []
