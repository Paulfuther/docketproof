import json
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from arl.msg.email_utils import (
    extract_sendgrid_event_subject,
    render_merge_fields,
    resolve_email_subject,
)
from arl.msg.models import EmailEvent, EmailLog, EmailTemplate
from arl.msg.tasks import master_email_send_task, process_sendgrid_webhook
from arl.user.models import CustomUser, Employer


class EmailUtilsTests(TestCase):
    def test_render_merge_fields_escapes_double_braces(self):
        html = "<p>Hello {{name}}</p>"
        rendered = render_merge_fields(html, {"name": "<b>Pat</b>"})
        self.assertEqual(rendered, "<p>Hello &lt;b&gt;Pat&lt;/b&gt;</p>")

    def test_render_merge_fields_triple_braces_unescaped(self):
        html = "<div>{{{body}}}</div>"
        rendered = render_merge_fields(html, {"body": "<strong>Hi</strong>"})
        self.assertEqual(rendered, "<div><strong>Hi</strong></div>")

    def test_resolve_subject_prefers_user_input(self):
        template = EmailTemplate(name="Template Name", subject="Template Subject")
        employer = Employer(name="Acme")
        self.assertEqual(
            resolve_email_subject(
                subject="User Subject", template=template, employer=employer
            ),
            "User Subject",
        )

    def test_resolve_subject_falls_back_to_template_then_name(self):
        template = EmailTemplate(name="Welcome pack", subject="  Hello {{name}}  ")
        self.assertEqual(
            resolve_email_subject(template=template),
            "Hello {{name}}",
        )
        template.subject = ""
        self.assertEqual(resolve_email_subject(template=template), "Welcome pack")

    def test_extract_subject_from_unique_args(self):
        self.assertEqual(
            extract_sendgrid_event_subject(
                {"unique_args": {"subject": "From unique args"}}
            ),
            "From unique args",
        )
        self.assertEqual(
            extract_sendgrid_event_subject(
                {"custom_args": json.dumps({"subject": "From json"})}
            ),
            "From json",
        )
        self.assertEqual(
            extract_sendgrid_event_subject({"subject": "Native"}),
            "Native",
        )


class EmailLogSubjectTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(
            name="Acme Co",
            senior_contact_name="Alex Manager",
        )

    @patch("arl.msg.tasks.create_master_email")
    def test_generic_template_send_persists_subject_on_email_log(self, mock_send):
        mock_send.return_value = True
        result = master_email_send_task.run(
            recipients=[{"name": "Pat Doe", "email": "pat@example.com"}],
            sendgrid_id="d-legacy-template-id",
            employer_id=self.employer.id,
            subject="Safety reminder",
            template_name="Safety Reminder",
        )
        self.assertIn("successfully", result.lower())
        log = EmailLog.objects.get()
        self.assertEqual(log.subject, "Safety reminder")
        self.assertEqual(log.template_name, "Safety Reminder")
        self.assertEqual(log.employer, self.employer)
        self.assertEqual(log.status, "SUCCESS")

        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["template_data"]["subject"], "Safety reminder")
        self.assertEqual(kwargs["custom_args"]["subject"], "Safety reminder")

    @patch("arl.msg.tasks.create_master_email")
    def test_in_app_html_is_rendered_and_sent_as_html_content(self, mock_send):
        mock_send.return_value = True
        master_email_send_task.run(
            recipients=[{"name": "Pat Doe", "email": "pat@example.com"}],
            sendgrid_id="",
            employer_id=self.employer.id,
            subject="Hi {{name}}",
            html_body="<p>Welcome to {{company_name}}</p>",
            template_name="Welcome",
        )
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["html_content"], "<p>Welcome to Acme Co</p>")
        self.assertEqual(kwargs["template_data"]["subject"], "Hi Pat Doe")
        self.assertEqual(EmailLog.objects.get().subject, "Hi {{name}}")

    def test_webhook_stores_subject_from_unique_args(self):
        payload = [
            {
                "email": "pat@example.com",
                "event": "delivered",
                "sg_event_id": "evt-subject-1",
                "sg_message_id": "msg-subject-1",
                "sg_template_id": "d-legacy-template-id",
                "sg_template_name": "Safety Reminder",
                "timestamp": int(timezone.now().timestamp()),
                "unique_args": {"subject": "Safety reminder"},
            }
        ]
        process_sendgrid_webhook.run(payload)
        event = EmailEvent.objects.get(sg_event_id="evt-subject-1")
        self.assertEqual(event.subject, "Safety reminder")


class InAppEmailTemplateViewTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme Co")
        self.email_group = Group.objects.create(name="SendEMAIL")
        self.user = CustomUser.objects.create_user(
            username="hr.user",
            email="hr@example.com",
            password="pass12345",
            phone_number="+15195550100",
            employer=self.employer,
            first_name="Pat",
            last_name="Doe",
        )
        self.user.groups.add(self.email_group)
        self.client.force_login(self.user)

    def test_create_in_app_template(self):
        response = self.client.post(
            reverse("email_template_create"),
            {
                "name": "Welcome",
                "subject": "Hello {{name}}",
                "html_body": "<p>Welcome to {{company_name}}</p>",
            },
        )
        self.assertEqual(response.status_code, 302)
        template = EmailTemplate.objects.get(name="Welcome")
        self.assertEqual(template.subject, "Hello {{name}}")
        self.assertTrue(template.is_in_app)
        self.assertIn(self.employer, template.employers.all())

    def test_preview_renders_merge_fields(self):
        template = EmailTemplate.objects.create(
            name="Welcome",
            subject="Hello {{name}}",
            html_body="<p>From {{company_name}}</p>",
        )
        template.employers.add(self.employer)
        response = self.client.get(
            reverse("email_template_preview", args=[template.pk])
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["subject"], "Hello Pat Doe")
        self.assertIn("Acme Co", data["html"])
        self.assertTrue(data["is_in_app"])

    @patch("arl.msg.views.master_email_send_task")
    def test_comms_template_send_passes_subject(self, mock_task):
        comms_group = Group.objects.create(name="SendCOMMS")
        self.user.groups.add(comms_group)
        recipient = CustomUser.objects.create_user(
            username="worker",
            email="worker@example.com",
            password="pass12345",
            phone_number="+15195550101",
            employer=self.employer,
            first_name="Sam",
            last_name="Lee",
            is_active=True,
        )
        template = EmailTemplate.objects.create(
            name="Policy update",
            subject="Please read",
            html_body="<p>Hello {{name}}</p>",
        )
        template.employers.add(self.employer)

        response = self.client.post(
            reverse("comms") + "?tab=email",
            {
                "form_type": "email",
                "email_mode": "template",
                "sendgrid_id": str(template.pk),
                "selected_users": [str(recipient.pk)],
                "subject": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(mock_task.delay.called)
        kwargs = mock_task.delay.call_args.kwargs
        self.assertEqual(kwargs["subject"], "Please read")
        self.assertEqual(kwargs["template_name"], "Policy update")
        self.assertIn("<p>Hello {{name}}</p>", kwargs["html_body"])

