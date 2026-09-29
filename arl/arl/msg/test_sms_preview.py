from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from arl.msg.helpers import SMS_OPT_OUT_FOOTER, with_sms_opt_out
from arl.msg.models import SmsLog
from arl.msg.tasks import send_sms_to_selected_users_task
from arl.setup.models import TenantApiKeys
from arl.user.models import CustomUser, Employer


class SmsOptOutFooterTests(TestCase):
    def test_compose_helper_appends_stop_footer(self):
        self.assertEqual(
            with_sms_opt_out("Shift starts at 9."),
            f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}",
        )
        self.assertEqual(SMS_OPT_OUT_FOOTER, "Reply STOP to opt out.")

    def test_compose_helper_does_not_duplicate_footer(self):
        already = f"See you there. {SMS_OPT_OUT_FOOTER}"
        self.assertEqual(with_sms_opt_out(already), already)
        self.assertEqual(with_sms_opt_out("  "), "")

    def test_sms_form_preview_includes_stop_footer(self):
        html = (
            Path(__file__).resolve().parent.parent
            / "templates"
            / "msg"
            / "sms_form.html"
        ).read_text()
        self.assertIn('id="sms-preview"', html)
        self.assertIn(f'data-opt-out-footer="{SMS_OPT_OUT_FOOTER}"', html)
        self.assertIn("updateSmsPreview", html)
        self.assertIn('getElementById("id_sms_message")', html)

    def test_comms_sms_tab_renders_preview(self):
        employer = Employer.objects.create(name="Acme Co")
        user = CustomUser.objects.create_user(
            username="sms.sender",
            email="sms@example.com",
            password="pass12345",
            phone_number="+15195550100",
            employer=employer,
            first_name="Pat",
            last_name="Doe",
        )
        for name in ("SendCOMMS", "SendSMS"):
            user.groups.add(Group.objects.create(name=name))
        self.client.force_login(user)

        response = self.client.get(reverse("comms") + "?tab=sms")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('id="sms-preview"', body)
        self.assertIn(SMS_OPT_OUT_FOOTER, body)
        self.assertIn('id="id_sms_message"', body)

    @patch("arl.msg.tasks.send_bulk_sms")
    def test_selected_users_task_sends_stop_footer(self, mock_send):
        mock_send.return_value = True
        employer = Employer.objects.create(name="Acme Co")
        sender = CustomUser.objects.create_user(
            username="sms.sender",
            email="sms@example.com",
            password="pass12345",
            phone_number="+15195550100",
            employer=employer,
        )
        recipient = CustomUser.objects.create_user(
            username="sms.recipient",
            email="worker@example.com",
            password="pass12345",
            phone_number="+15195550101",
            employer=employer,
        )
        TenantApiKeys.objects.create(
            employer=employer,
            account_sid="AC123",
            auth_token="token",
            notify_service_sid="IS123",
            is_active=True,
        )

        send_sms_to_selected_users_task.run(
            [recipient.pk],
            "Shift starts at 9.",
            sender.pk,
        )

        self.assertTrue(mock_send.called)
        sent_body = mock_send.call_args.args[1]
        self.assertEqual(sent_body, f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}")

        log = SmsLog.objects.get()
        self.assertEqual(log.level, "INFO")
        self.assertIn(sent_body, log.message)
        self.assertIn(SMS_OPT_OUT_FOOTER, log.message)
