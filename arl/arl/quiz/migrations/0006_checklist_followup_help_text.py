import arl.quiz.models
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("quiz", "0005_checklist_action_plan_and_responsibility"),
    ]

    operations = [
        migrations.AlterField(
            model_name="checklisttemplateitem",
            name="responsibility_assignable",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Show L/S for this item. L/S is required only when the answer "
                    "is listed in create_action_on. Yes does not require L/S unless Y is listed."
                ),
            ),
        ),
        migrations.AlterField(
            model_name="checklisttemplateitem",
            name="create_action_on",
            field=models.JSONField(
                blank=True,
                default=arl.quiz.models.default_create_action_on,
                help_text=(
                    "Answers that require a follow-up. "
                    '["N"] when No is the problem, ["Y"] when Yes is the problem, '
                    "[] when neither needs a follow-up."
                ),
            ),
        ),
    ]
