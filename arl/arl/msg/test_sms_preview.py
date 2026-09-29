from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from arl.msg.helpers import (
    SMS_OPT_OUT_FOOTER,
    compose_outbound_sms,
    send_bulk_sms,
    sms_compose_greeting,
    with_sms_opt_out,
)
from arl.msg.models import SmsLog
from arl.msg.tasks import (
    send_one_off_bulk_sms_task,
    send_sms_to_selected_users_task,
)
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

    def test_compose_outbound_sms_greeting_body_and_stop(self):
        employer = Employer(name="Acme Co", senior_contact_name="Pat Manager")
        body = compose_outbound_sms("Shift starts at 9.", employer)
        self.assertEqual(
            body,
            "Hello, this is Pat Manager from Acme Co.\n"
            f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}",
        )
        self.assertEqual(
            sms_compose_greeting(employer),
            "Hello, this is Pat Manager from Acme Co.",
        )
        self.assertTrue(body.endswith(SMS_OPT_OUT_FOOTER))

    def test_compose_outbound_sms_blank_names(self):
        employer = Employer(name="   ", senior_contact_name="  ")
        body = compose_outbound_sms("Shift starts at 9.", employer)
        self.assertEqual(
            body,
            "Hello, this is your contact from our company.\n"
            f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}",
        )
        self.assertEqual(
            compose_outbound_sms("", None),
            f"Hello, this is your contact from our company.\n{SMS_OPT_OUT_FOOTER}",
        )

    def test_sms_form_preview_includes_greeting_and_stop_footer(self):
        html = (
            Path(__file__).resolve().parent.parent
            / "templates"
            / "msg"
            / "sms_form.html"
        ).read_text()
        self.assertIn('id="sms-preview"', html)
        self.assertIn(f'data-opt-out-footer="{SMS_OPT_OUT_FOOTER}"', html)
        self.assertIn('data-sms-greeting="{{ sms_form.preview_greeting }}"', html)
        self.assertIn("updateSmsPreview", html)
        self.assertIn('getElementById("id_sms_message")', html)
        self.assertIn("data-sms-greeting", html)
        self.assertIn('greeting + "\\n" + text', html)

    def test_comms_sms_tab_renders_preview(self):
        employer = Employer.objects.create(
            name="Acme Co", senior_contact_name="Pat Manager"
        )
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
        greeting = "Hello, this is Pat Manager from Acme Co."
        self.assertIn('id="sms-preview"', body)
        self.assertIn(SMS_OPT_OUT_FOOTER, body)
        self.assertIn('id="id_sms_message"', body)
        self.assertIn(f'data-sms-greeting="{greeting}"', body)

    @patch("arl.msg.helpers.Client")
    def test_send_bulk_sms_appends_stop_before_notify(self, mock_client_cls):
        notifications = (
            mock_client_cls.return_value.notify.services.return_value.notifications
        )
        notifications.create.return_value = object()

        sent = send_bulk_sms(
            ["+15195550101"],
            "Shift starts at 9.",
            "AC123",
            "token",
            "IS123",
        )

        self.assertTrue(sent)
        body = notifications.create.call_args.kwargs["body"]
        self.assertEqual(body, f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}")
        self.assertTrue(body.endswith(SMS_OPT_OUT_FOOTER))

        notifications.create.reset_mock()
        send_bulk_sms(
            ["+15195550101"],
            f"Already opted.\n{SMS_OPT_OUT_FOOTER}",
            "AC123",
            "token",
            "IS123",
        )
        again = notifications.create.call_args.kwargs["body"]
        self.assertEqual(again.count(SMS_OPT_OUT_FOOTER), 1)
        self.assertTrue(again.endswith(SMS_OPT_OUT_FOOTER))

    @patch("arl.msg.tasks.send_bulk_sms")
    def test_selected_users_task_sends_greeting_and_stop(self, mock_send):
        mock_send.return_value = True
        employer = Employer.objects.create(
            name="Acme Co", senior_contact_name="Pat Manager"
        )
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
        self.assertEqual(
            sent_body,
            "Hello, this is Pat Manager from Acme Co.\n"
            f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}",
        )
        self.assertTrue(sent_body.endswith(SMS_OPT_OUT_FOOTER))

        log = SmsLog.objects.get()
        self.assertEqual(log.level, "INFO")
        self.assertIn(sent_body, log.message)
        self.assertIn(SMS_OPT_OUT_FOOTER, log.message)
        field_names = {field.name for field in SmsLog._meta.fields}
        self.assertIn("level", field_names)
        self.assertIn("message", field_names)
        self.assertNotIn("message_body", field_names)

    @patch("arl.msg.tasks.send_bulk_sms")
    def test_group_send_uses_compose_helper(self, mock_send):
        mock_send.return_value = True
        employer = Employer.objects.create(name="", senior_contact_name="")
        sender = CustomUser.objects.create_user(
            username="sms.group.sender",
            email="group@example.com",
            password="pass12345",
            phone_number="+15195550102",
            employer=employer,
        )
        recipient = CustomUser.objects.create_user(
            username="sms.group.recipient",
            email="crew@example.com",
            password="pass12345",
            phone_number="+15195550103",
            employer=employer,
        )
        group = Group.objects.create(name="Crew")
        recipient.groups.add(group)
        TenantApiKeys.objects.create(
            employer=employer,
            account_sid="AC123",
            auth_token="token",
            notify_service_sid="IS123",
            is_active=True,
        )

        send_one_off_bulk_sms_task.run(group.pk, "Shift starts at 9.", sender.pk)

        sent_body = mock_send.call_args.args[1]
        self.assertEqual(
            sent_body,
            "Hello, this is your contact from our company.\n"
            f"Shift starts at 9.\n{SMS_OPT_OUT_FOOTER}",
        )
        log = SmsLog.objects.get()
        self.assertEqual(log.level, "INFO")
        self.assertIn(sent_body, log.message)
        self.assertNotIn("message_body", {field.name for field in SmsLog._meta.fields})
