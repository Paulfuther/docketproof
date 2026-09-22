import json
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from arl.msg.email_utils import (
    COMPOSE_TEMPLATE_NAME,
    EMAIL_IMAGE_MAX_WIDTH,
    EMAIL_SOURCE_COMPOSE,
    EMAIL_SOURCE_IN_APP,
    EMAIL_SOURCE_SENDGRID,
    GENERIC_SENDGRID_TEMPLATE_ID,
    email_identity,
    extract_sendgrid_event_meta,
    extract_sendgrid_event_subject,
    prepare_email_image,
    render_merge_fields,
    resolve_email_subject,
    wrap_in_app_email_html,
)
from arl.msg.models import EmailEvent, EmailLog, EmailTemplate
from arl.msg.tasks import (
    generate_email_event_summary,
    generate_employee_email_report_task,
    master_email_send_task,
    process_sendgrid_webhook,
)
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

    def test_extract_meta_prefers_unique_args_for_in_app(self):
        meta = extract_sendgrid_event_meta(
            {
                "sg_template_id": GENERIC_SENDGRID_TEMPLATE_ID,
                "sg_template_name": "Generic wrapper",
                "unique_args": {
                    "subject": "Policy update",
                    "template_name": "Policy update",
                    "source": EMAIL_SOURCE_IN_APP,
                    "app_template_id": "42",
                },
            }
        )
        self.assertEqual(meta["subject"], "Policy update")
        self.assertEqual(meta["template_name"], "Policy update")
        self.assertEqual(meta["source"], EMAIL_SOURCE_IN_APP)
        self.assertEqual(meta["sendgrid_id"], "")
        self.assertEqual(meta["app_template_id"], 42)

    def test_extract_meta_keeps_legacy_sendgrid_native_fields(self):
        meta = extract_sendgrid_event_meta(
            {
                "sg_template_id": "d-legacy-template-id",
                "sg_template_name": "Safety Reminder",
                "unique_args": {"subject": "Safety reminder"},
            }
        )
        self.assertEqual(meta["template_name"], "Safety Reminder")
        self.assertEqual(meta["sendgrid_id"], "d-legacy-template-id")
        self.assertEqual(meta["source"], EMAIL_SOURCE_SENDGRID)

    def test_extract_meta_does_not_infer_compose_on_old_events(self):
        meta = extract_sendgrid_event_meta(
            {
                "event": "delivered",
                "unique_args": {"subject": "Hello"},
            }
        )
        self.assertEqual(meta["source"], "")
        self.assertEqual(meta["template_name"], "")

    def test_extract_meta_compose_keeps_wrapper_id(self):
        meta = extract_sendgrid_event_meta(
            {
                "sg_template_id": GENERIC_SENDGRID_TEMPLATE_ID,
                "sg_template_name": "Generic wrapper",
                "unique_args": {
                    "subject": "Hello team",
                    "template_name": COMPOSE_TEMPLATE_NAME,
                    "source": EMAIL_SOURCE_COMPOSE,
                    "sendgrid_id": GENERIC_SENDGRID_TEMPLATE_ID,
                },
            }
        )
        self.assertEqual(meta["template_name"], COMPOSE_TEMPLATE_NAME)
        self.assertEqual(meta["source"], EMAIL_SOURCE_COMPOSE)
        self.assertEqual(meta["sendgrid_id"], GENERIC_SENDGRID_TEMPLATE_ID)

    def test_email_identity_in_app_omits_generic_wrapper_id(self):
        identity = email_identity(
            html_body="<p>Hi</p>",
            sendgrid_id="",
            template_name="Welcome",
            app_template_id=9,
        )
        self.assertEqual(identity["source"], EMAIL_SOURCE_IN_APP)
        self.assertEqual(identity["template_name"], "Welcome")
        self.assertEqual(identity["sendgrid_id"], "")
        self.assertEqual(identity["app_template_id"], 9)

    def test_prepare_email_image_caps_width_and_keeps_aspect(self):
        img = Image.new("RGB", (1200, 800), color=(200, 10, 10))
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        out, ext, content_type = prepare_email_image(buf, filename="wide.png")
        result = Image.open(out)
        self.assertEqual(result.width, EMAIL_IMAGE_MAX_WIDTH)
        self.assertEqual(result.height, 400)
        self.assertEqual(ext, "jpg")
        self.assertEqual(content_type, "image/jpeg")

    def test_prepare_email_image_leaves_narrow_images(self):
        img = Image.new("RGB", (300, 180), color=(10, 10, 200))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        buf.seek(0)
        out, ext, _ctype = prepare_email_image(buf, filename="narrow.jpg")
        result = Image.open(out)
        self.assertEqual(result.width, 300)
        self.assertEqual(result.height, 180)
        self.assertEqual(ext, "jpg")

    def test_prepare_email_image_keeps_png_transparency(self):
        img = Image.new("RGBA", (800, 400), color=(255, 0, 0, 128))
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        out, ext, content_type = prepare_email_image(buf, filename="logo.png")
        result = Image.open(out)
        self.assertEqual(ext, "png")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(result.width, EMAIL_IMAGE_MAX_WIDTH)
        self.assertEqual(result.mode, "RGBA")

    def test_wrap_header_image_above_body(self):
        wrapped = wrap_in_app_email_html(
            "<p>Hello</p>", "https://cdn.example/header.jpg"
        )
        self.assertIn("https://cdn.example/header.jpg", wrapped)
        self.assertLess(wrapped.index("<img"), wrapped.index("<p>Hello</p>"))
        self.assertIn("max-width:600px", wrapped)
        self.assertEqual(wrap_in_app_email_html("<p>Hello</p>", ""), "<p>Hello</p>")
        self.assertEqual(wrap_in_app_email_html("<p>Hello</p>", None), "<p>Hello</p>")


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
        self.assertEqual(log.source, EMAIL_SOURCE_SENDGRID)
        self.assertEqual(log.template_id, "d-legacy-template-id")
        self.assertEqual(log.employer, self.employer)
        self.assertEqual(log.status, "SUCCESS")

        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["template_data"]["subject"], "Safety reminder")
        self.assertEqual(kwargs["custom_args"]["subject"], "Safety reminder")
        self.assertEqual(kwargs["custom_args"]["template_name"], "Safety Reminder")
        self.assertEqual(kwargs["custom_args"]["source"], EMAIL_SOURCE_SENDGRID)
        self.assertEqual(kwargs["custom_args"]["sendgrid_id"], "d-legacy-template-id")

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
            app_template_id=17,
        )
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["html_content"], "<p>Welcome to Acme Co</p>")
        self.assertEqual(kwargs["template_data"]["subject"], "Hi Pat Doe")
        self.assertEqual(kwargs["custom_args"]["template_name"], "Welcome")
        self.assertEqual(kwargs["custom_args"]["source"], EMAIL_SOURCE_IN_APP)
        self.assertNotIn("sendgrid_id", kwargs["custom_args"])
        self.assertEqual(kwargs["custom_args"]["app_template_id"], "17")
        log = EmailLog.objects.get()
        self.assertEqual(log.subject, "Hi {{name}}")
        self.assertEqual(log.source, EMAIL_SOURCE_IN_APP)
        self.assertEqual(log.template_name, "Welcome")
        self.assertEqual(log.template_id, "")

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
        self.assertEqual(event.sg_template_name, "Safety Reminder")
        self.assertEqual(event.sg_template_id, "d-legacy-template-id")
        self.assertEqual(event.source, EMAIL_SOURCE_SENDGRID)

    @patch("arl.msg.tasks.create_master_email")
    def test_compose_send_logs_as_compose_with_wrapper_id(self, mock_send):
        mock_send.return_value = True
        master_email_send_task.run(
            recipients=[{"name": "Pat Doe", "email": "pat@example.com"}],
            sendgrid_id=GENERIC_SENDGRID_TEMPLATE_ID,
            employer_id=self.employer.id,
            subject="Hello team",
            body="<p>One-off</p>",
            template_name=COMPOSE_TEMPLATE_NAME,
            source=EMAIL_SOURCE_COMPOSE,
        )
        log = EmailLog.objects.get()
        self.assertEqual(log.source, EMAIL_SOURCE_COMPOSE)
        self.assertEqual(log.template_name, COMPOSE_TEMPLATE_NAME)
        self.assertEqual(log.template_id, GENERIC_SENDGRID_TEMPLATE_ID)
        self.assertEqual(log.subject, "Hello team")
        kwargs = mock_send.call_args.kwargs
        self.assertEqual(kwargs["custom_args"]["source"], EMAIL_SOURCE_COMPOSE)
        self.assertEqual(kwargs["custom_args"]["template_name"], COMPOSE_TEMPLATE_NAME)
        self.assertEqual(
            kwargs["custom_args"]["sendgrid_id"], GENERIC_SENDGRID_TEMPLATE_ID
        )
        self.assertNotIn("app_template_id", kwargs["custom_args"])

    def test_webhook_in_app_unique_args_fill_template_fields(self):
        payload = [
            {
                "email": "pat@example.com",
                "event": "click",
                "sg_event_id": "evt-in-app-1",
                "sg_message_id": "msg-in-app-1",
                "timestamp": int(timezone.now().timestamp()),
                "unique_args": {
                    "subject": "Please read",
                    "template_name": "Policy update",
                    "source": EMAIL_SOURCE_IN_APP,
                    "app_template_id": "99",
                },
            }
        ]
        process_sendgrid_webhook.run(payload)
        event = EmailEvent.objects.get(sg_event_id="evt-in-app-1")
        self.assertEqual(event.subject, "Please read")
        self.assertEqual(event.sg_template_name, "Policy update")
        self.assertEqual(event.sg_template_id, "")
        self.assertEqual(event.source, EMAIL_SOURCE_IN_APP)
        self.assertEqual(event.app_template_id, 99)

    def test_webhook_old_event_without_unique_args_is_not_compose(self):
        payload = [
            {
                "email": "pat@example.com",
                "event": "delivered",
                "sg_event_id": "evt-old-1",
                "sg_message_id": "msg-old-1",
                "timestamp": int(timezone.now().timestamp()),
            }
        ]
        process_sendgrid_webhook.run(payload)
        event = EmailEvent.objects.get(sg_event_id="evt-old-1")
        self.assertEqual(event.source, "")
        self.assertEqual(event.sg_template_name, "")

    def test_employee_report_includes_audited_in_app_events(self):
        employee = CustomUser.objects.create_user(
            username="audited.user",
            email="audited@example.com",
            password="pass12345",
            phone_number="+15195550999",
            employer=self.employer,
        )
        template = EmailTemplate.objects.create(
            name="Policy update",
            subject="Please read",
            html_body="<p>Hello</p>",
            include_in_report=True,
        )
        template.employers.add(self.employer)
        ignored = EmailTemplate.objects.create(
            name="Ignored",
            subject="Nope",
            html_body="<p>Nope</p>",
            include_in_report=False,
        )
        EmailEvent.objects.create(
            email=employee.email,
            event="click",
            ip="192.0.2.1",
            sg_event_id="evt-audit-1",
            sg_message_id="msg-audit-1",
            sg_template_id="",
            sg_template_name="Policy update",
            source=EMAIL_SOURCE_IN_APP,
            app_template_id=template.pk,
            timestamp=timezone.now(),
            url="",
            username=employee.username,
        )
        EmailEvent.objects.create(
            email=employee.email,
            event="open",
            ip="192.0.2.1",
            sg_event_id="evt-audit-ignored",
            sg_message_id="msg-audit-ignored",
            sg_template_id="",
            sg_template_name="Ignored",
            source=EMAIL_SOURCE_IN_APP,
            app_template_id=ignored.pk,
            timestamp=timezone.now(),
            url="",
            username=employee.username,
        )
        html = generate_employee_email_report_task.run(employee.id)
        self.assertIn("Policy update", html)
        self.assertNotIn("Ignored", html)

    def test_employee_report_still_matches_legacy_sendgrid_ids(self):
        employee = CustomUser.objects.create_user(
            username="legacy.user",
            email="legacy@example.com",
            password="pass12345",
            phone_number="+15195550998",
            employer=self.employer,
        )
        EmailTemplate.objects.create(
            name="Safety Reminder",
            sendgrid_id="d-legacy-template-id",
            include_in_report=True,
        )
        EmailEvent.objects.create(
            email=employee.email,
            event="open",
            ip="192.0.2.1",
            sg_event_id="evt-legacy-audit",
            sg_message_id="msg-legacy-audit",
            sg_template_id="d-legacy-template-id",
            sg_template_name="Safety Reminder",
            source=EMAIL_SOURCE_SENDGRID,
            timestamp=timezone.now(),
            url="",
            username=employee.username,
        )
        html = generate_employee_email_report_task.run(employee.id)
        self.assertIn("Safety Reminder", html)

    def test_event_summary_matches_in_app_template_by_app_id(self):
        template = EmailTemplate.objects.create(
            name="Policy update",
            subject="Please read",
            html_body="<p>Hello</p>",
            include_in_report=True,
        )
        EmailEvent.objects.create(
            email="pat@example.com",
            event="click",
            ip="192.0.2.1",
            sg_event_id="evt-summary-in-app",
            sg_message_id="msg-summary-in-app",
            sg_template_id="",
            sg_template_name="Policy update",
            source=EMAIL_SOURCE_IN_APP,
            app_template_id=template.pk,
            employer=self.employer,
            timestamp=timezone.now(),
            url="",
            username="pat",
        )
        html = generate_email_event_summary.run(
            template_id="",
            employer_id=self.employer.id,
            app_template_id=template.pk,
        )
        self.assertIn("Policy update", html)
        self.assertIn("pat@example.com", html)


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
        self.assertFalse(template.include_in_report)
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
        self.assertEqual(kwargs["source"], EMAIL_SOURCE_IN_APP)
        self.assertEqual(kwargs["app_template_id"], template.pk)
        self.assertIn("<p>Hello {{name}}</p>", kwargs["html_body"])

    def test_preview_and_send_include_header_image(self):
        template = EmailTemplate.objects.create(
            name="Welcome",
            subject="Hello {{name}}",
            html_body="<p>From {{company_name}}</p>",
            header_image_url="https://cdn.example/header.jpg",
        )
        template.employers.add(self.employer)
        response = self.client.get(
            reverse("email_template_preview", args=[template.pk])
        )
        data = response.json()
        self.assertIn("https://cdn.example/header.jpg", data["html"])
        self.assertTrue(
            data["html"].index("https://cdn.example/header.jpg")
            < data["html"].index("From Acme Co")
        )
        self.assertEqual(data["header_image_url"], "https://cdn.example/header.jpg")

    def test_save_and_clear_header_image_url(self):
        response = self.client.post(
            reverse("email_template_create"),
            {
                "name": "Branded",
                "subject": "Hello",
                "html_body": "<p>Body</p>",
                "header_image_url": "https://cdn.example/header.jpg",
            },
        )
        self.assertEqual(response.status_code, 302)
        template = EmailTemplate.objects.get(name="Branded")
        self.assertEqual(template.header_image_url, "https://cdn.example/header.jpg")

        response = self.client.post(
            reverse("email_template_edit", args=[template.pk]),
            {
                "name": "Branded",
                "subject": "Hello",
                "html_body": "<p>Body</p>",
                "header_image_url": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        template.refresh_from_db()
        self.assertEqual(template.header_image_url, "")

    def test_include_in_report_persists_and_defaults_off(self):
        response = self.client.post(
            reverse("email_template_create"),
            {
                "name": "Audited",
                "subject": "Hello",
                "html_body": "<p>Body</p>",
                "include_in_report": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        template = EmailTemplate.objects.get(name="Audited")
        self.assertTrue(template.include_in_report)

        response = self.client.post(
            reverse("email_template_edit", args=[template.pk]),
            {
                "name": "Audited",
                "subject": "Hello",
                "html_body": "<p>Body</p>",
            },
        )
        self.assertEqual(response.status_code, 302)
        template.refresh_from_db()
        self.assertFalse(template.include_in_report)

    @patch("arl.msg.views.master_email_send_task")
    def test_comms_compose_send_logs_as_compose(self, mock_task):
        comms_group, _created = Group.objects.get_or_create(name="SendCOMMS")
        self.user.groups.add(comms_group)
        recipient = CustomUser.objects.create_user(
            username="worker3",
            email="worker3@example.com",
            password="pass12345",
            phone_number="+15195550103",
            employer=self.employer,
            first_name="Sam",
            last_name="Lee",
            is_active=True,
        )
        response = self.client.post(
            reverse("comms") + "?tab=email",
            {
                "form_type": "email",
                "email_mode": "text",
                "selected_users": [str(recipient.pk)],
                "subject": "One-off subject",
                "message": "Hello from compose",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(mock_task.delay.called)
        kwargs = mock_task.delay.call_args.kwargs
        self.assertEqual(kwargs["subject"], "One-off subject")
        self.assertEqual(kwargs["template_name"], COMPOSE_TEMPLATE_NAME)
        self.assertEqual(kwargs["source"], EMAIL_SOURCE_COMPOSE)
        self.assertEqual(kwargs["sendgrid_id"], GENERIC_SENDGRID_TEMPLATE_ID)
        self.assertNotIn("app_template_id", kwargs)

    @patch("arl.msg.helpers.SendGridAPIClient")
    def test_in_app_html_enables_click_and_open_tracking(self, mock_client):
        mock_client.return_value.send.return_value.status_code = 202
        from arl.msg.helpers import create_master_email

        ok = create_master_email(
            to_email="pat@example.com",
            sendgrid_id="",
            template_data={"subject": "Hello", "name": "Pat"},
            verified_sender="noreply@example.com",
            custom_args={
                "subject": "Hello",
                "template_name": "Welcome",
                "source": EMAIL_SOURCE_IN_APP,
                "app_template_id": "12",
            },
            html_content='<p>Hi <a href="https://example.com/policy">policy</a></p>',
        )
        self.assertTrue(ok)
        mail = mock_client.return_value.send.call_args[0][0]
        payload = mail.get()
        self.assertTrue(payload["tracking_settings"]["click_tracking"]["enable"])
        self.assertTrue(payload["tracking_settings"]["open_tracking"]["enable"])
        custom = payload["personalizations"][0]["custom_args"]
        self.assertEqual(custom["app_template_id"], "12")
        self.assertEqual(custom["source"], EMAIL_SOURCE_IN_APP)

    @patch("arl.msg.views.master_email_send_task")
    def test_comms_send_wraps_header_image(self, mock_task):
        comms_group, _created = Group.objects.get_or_create(name="SendCOMMS")
        self.user.groups.add(comms_group)
        recipient = CustomUser.objects.create_user(
            username="worker2",
            email="worker2@example.com",
            password="pass12345",
            phone_number="+15195550102",
            employer=self.employer,
            first_name="Sam",
            last_name="Lee",
            is_active=True,
        )
        template = EmailTemplate.objects.create(
            name="Header send",
            subject="Please read",
            html_body="<p>Hello {{name}}</p>",
            header_image_url="https://cdn.example/banner.jpg",
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
        kwargs = mock_task.delay.call_args.kwargs
        self.assertIn("https://cdn.example/banner.jpg", kwargs["html_body"])
        self.assertIn("<p>Hello {{name}}</p>", kwargs["html_body"])

