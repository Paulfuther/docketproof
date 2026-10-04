import re
from pathlib import Path

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from arl.user.models import CustomUser, Employer


VISIBLE_LABELS = (
    "Email",
    "Manage templates",
    "SMS",
    "DocuSign",
    "Email logs",
    "SMS link logs",
)


def _attr_count(html, name, value):
    """Count a real attribute, so data-title does not also match title."""
    return len(
        re.findall(
            rf'(?<![\w-]){re.escape(name)}="{re.escape(value)}"',
            html,
        )
    )


class CommsNavTooltipTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme Co")
        self.user = CustomUser.objects.create_user(
            username="comms.user",
            email="comms@example.com",
            password="pass12345",
            phone_number="+15195550100",
            employer=self.employer,
            first_name="Pat",
            last_name="Doe",
        )
        for name in (
            "SendCOMMS",
            "SendEMAIL",
            "SendSMS",
            "SendDOCUSIGN",
            "EmailLOGS",
            "SmsLOGS",
        ):
            self.user.groups.add(Group.objects.create(name=name))
        self.client.force_login(self.user)

    def test_side_menu_tooltips_and_mobile_labels(self):
        response = self.client.get(reverse("comms"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()

        desktop = re.search(
            r'<aside class="d-none d-lg-block col-lg-1 px-0">.*?</aside>',
            body,
            re.S,
        )
        mobile = re.search(
            r'<div id="commsNavMobile".*?</div>\s*<!-- ===== Content ===== -->',
            body,
            re.S,
        )
        self.assertIsNone(desktop)
        self.assertIsNotNone(mobile)
        desktop_html = ""
        mobile_html = mobile.group(0)

        for label in VISIBLE_LABELS:
            for chunk, where in ((mobile_html, "mobile"),):
                for attr in ("data-title", "title", "aria-label"):
                    self.assertEqual(
                        _attr_count(chunk, attr, label),
                        1,
                        f"{where} {attr}={label}",
                    )
            for attr in ("data-title", "title", "aria-label"):
                self.assertEqual(
                    _attr_count(body, attr, label),
                    1,
                    f"page {attr}={label}",
                )
            self.assertIn(
                f'<span class="comms-nav-label" aria-hidden="true">{label}</span>',
                mobile_html,
            )

        self.assertEqual(mobile_html.count("comms-nav-label"), len(VISIBLE_LABELS))
        self.assertNotIn('data-title="DSign"', body)
        self.assertNotIn('data-title="SMS Link Logs"', body)
        self.assertNotIn("WhatsApp", desktop_html)
        self.assertNotIn("WhatsApp", mobile_html)

        self.assertIn("bootstrap.Tooltip", body)
        self.assertIn("(hover: hover) and (pointer: fine)", body)
        self.assertIn("comms-nav-tooltip", body)
        self.assertIn('trigger: "manual"', body)
        self.assertIn('el.closest("#commsNav") ? "right" : "bottom"', body)
        self.assertIn(":focus-visible", body)
        self.assertIn("removeAttribute(\"title\")", body)
        self.assertIn('url.searchParams.set("tab", tabName)', body)
        self.assertIn('aria-hidden="true"', mobile_html)
        self.assertIn('href="', mobile_html)
        self.assertEqual(body.count("comms-nav-link active"), 1)

        templates_dir = (
            Path(__file__).resolve().parent.parent / "templates" / "msg"
        )
        page = (templates_dir / "master_comms.html").read_text()
        template = (templates_dir / "partials" / "comms_side_nav.html").read_text()
        self.assertIn('{% include "msg/partials/comms_side_nav.html" %}', page)
        self.assertEqual(_attr_count(template, "data-title", "WhatsApp"), 2)
        self.assertEqual(_attr_count(template, "title", "WhatsApp"), 2)
        self.assertEqual(_attr_count(template, "aria-label", "WhatsApp"), 2)
        self.assertEqual(template.count(">WhatsApp</span>"), 1)
        self.assertIn("{% if can_send_whatsapp %}", template)
        self.assertIn("{% if can_send_whatsapp %}", page)
        self.assertNotIn('data-title="DSign"', template)
        self.assertNotIn('data-title="SMS Link Logs"', template)
        self.assertIn(".comms-nav-tooltip", template)
        self.assertIn("pointer-events: none", template)
