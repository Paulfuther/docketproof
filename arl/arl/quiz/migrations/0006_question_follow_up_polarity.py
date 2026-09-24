from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("quiz", "0005_checklist_action_plan_and_responsibility"),
    ]

    operations = [
        migrations.AddField(
            model_name="question",
            name="follow_up_on_no",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Check only when No needs a follow-up. "
                    "Leave off when No is an acceptable answer."
                ),
                verbose_name="Follow-up needed when answer is No",
            ),
        ),
        migrations.AddField(
            model_name="question",
            name="follow_up_on_yes",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Check when Yes needs a follow-up. "
                    "Leave off when Yes needs nothing else."
                ),
                verbose_name="Follow-up needed when answer is Yes",
            ),
        ),
        migrations.AddField(
            model_name="question",
            name="responsibility_assignable",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "L/S is required only for an answer that needs a follow-up. "
                    "Yes does not require L unless follow-up on Yes is checked."
                ),
                verbose_name="Require L or S when that follow-up applies",
            ),
        ),
    ]
