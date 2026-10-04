"""Salt logs, incidents, and checklists stay on three routes and share one chrome."""

from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arl.incident.models import Incident
from arl.quiz.models import Checklist, ChecklistTemplate, SaltLog
from arl.quiz.views import CHECKLIST_PAGE_SIZE
from arl.user.models import CustomUser, Employer, Store


def _grant_incident(user):
    content_type = ContentType.objects.get_for_model(Incident)
    perms = Permission.objects.filter(
        content_type=content_type,
        codename__in=("add_incident", "view_incident"),
    )
    user.user_permissions.add(*list(perms))


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class RecordHubChromeTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Test")
        self.other = Employer.objects.create(name="Other Co")
        self.user = CustomUser.objects.create_user(
            username="hub-gsa",
            email="hub-gsa@example.com",
            password="pass12345",
            phone_number="+15196707491",
            employer=self.employer,
        )
        self.other_user = CustomUser.objects.create_user(
            username="hub-other",
            email="hub-other@example.com",
            password="pass12345",
            phone_number="+15196707492",
            employer=self.other,
        )
        self.store = Store.objects.create(
            number=12,
            employer=self.employer,
            address="12 Main St",
            city="London",
            province="ON",
        )
        self.other_store = Store.objects.create(
            number=99,
            employer=self.other,
            address="99 Other St",
            city="Toronto",
            province="ON",
        )
        _grant_incident(self.user)
        self.client.force_login(self.user)
        self.reports = patch(
            "arl.incident.views.process_new_incident_reports_task.delay"
        )
        self.reports.start()
        self.addCleanup(self.reports.stop)

    def test_three_pages_stay_on_their_own_routes(self):
        salt = reverse("salt_log_list")
        incidents = reverse("incidents")
        checklists = reverse("checklist_dashboard")
        self.assertNotEqual(salt, incidents)
        self.assertNotEqual(salt, checklists)
        self.assertNotEqual(incidents, checklists)

        salt_page = self.client.get(salt)
        incident_page = self.client.get(incidents)
        checklist_page = self.client.get(checklists)
        self.assertEqual(salt_page.status_code, 200)
        self.assertEqual(incident_page.status_code, 200)
        self.assertEqual(checklist_page.status_code, 200)

        self.assertNotContains(salt_page, 'class="record-hub-title"')
        self.assertContains(salt_page, "Start salt log")
        self.assertContains(salt_page, reverse("create_salt_log"))
        self.assertContains(salt_page, 'href="#start"')
        self.assertContains(salt_page, 'href="#edit"')
        self.assertContains(salt_page, "record-hub-create")
        self.assertContains(salt_page, "btn-outline-secondary")
        self.assertNotContains(salt_page, "btn-primary")
        self.assertNotContains(salt_page, "Create site incident")

        self.assertNotContains(incident_page, 'class="record-hub-title"')
        self.assertContains(incident_page, 'href="#start"')
        self.assertContains(incident_page, 'href="#edit"')
        self.assertContains(incident_page, "Create site incident")
        self.assertNotContains(incident_page, "Start salt log")

        self.assertNotContains(checklist_page, 'class="record-hub-title"')
        self.assertContains(checklist_page, 'href="#start"')
        self.assertContains(checklist_page, 'href="#inprogress"')
        self.assertContains(checklist_page, 'href="#submitted"')
        self.assertContains(checklist_page, "In progress <br>")
        self.assertContains(checklist_page, "Submitted <br>")
        self.assertNotContains(checklist_page, 'href="#saved"')
        self.assertNotContains(checklist_page, "Start salt log")
        self.assertNotContains(checklist_page, "Create site incident")
        self.assertNotContains(checklist_page, "incident-pill")

    def test_shared_title_search_row_and_card_classes(self):
        SaltLog.objects.create(
            user=self.user,
            user_employer=self.employer,
            store=self.store,
            area_salted="Front walk",
            status=SaltLog.STATUS_DRAFT,
            image_folder="front-walk",
        )
        Incident.objects.create(
            store=self.store,
            user_employer=self.employer,
            brief_description="Fuel spill",
            eventdetails="Details",
            actionstaken="Cleaned up",
            eventdate=date(2026, 3, 1),
        )
        ChecklistTemplate.objects.create(
            name="Workplace Inspection", description="Weekly walk"
        )
        Checklist.objects.create(
            title="Store 12 inspection",
            created_by=self.user,
            store=self.store,
            status="draft",
        )

        pages = [
            self.client.get(reverse("salt_log_list")),
            self.client.get(reverse("incidents") + "?tab=edit"),
            self.client.get(reverse("checklist_dashboard") + "?tab=inprogress"),
        ]
        for page in pages:
            self.assertNotContains(page, 'class="record-hub-title"')
            self.assertContains(page, "font-size: 1.125rem")
            self.assertContains(page, "font-weight: 500")
            self.assertContains(page, "record-hub-tabs")
            self.assertContains(page, "record-hub-search")
            self.assertContains(page, "record-hub-card")
            self.assertContains(page, "record-list")
            self.assertContains(page, "record-row-title")
            self.assertContains(page, "record-row-meta")
            self.assertContains(page, "padding: 1rem")

        salt_page, incident_page, checklist_page = pages
        self.assertContains(salt_page, "Front walk")
        self.assertContains(salt_page, "Store 12 · London")
        self.assertContains(incident_page, "Fuel spill")
        self.assertContains(incident_page, "Store 12 · London · 2026-03-01")
        self.assertContains(checklist_page, "Store 12 inspection")
        self.assertContains(checklist_page, "Store 12 · London")

    def test_incidents_keep_live_search_and_tinted_pills(self):
        Incident.objects.create(
            store=self.store,
            user_employer=self.employer,
            brief_description="Fuel spill",
            eventdetails="Details",
            actionstaken="Cleaned up",
            eventdate=date(2026, 3, 2),
        )
        page = self.client.get(reverse("incidents"), {"tab": "edit"})
        self.assertContains(page, 'hx-trigger="input changed delay:300ms, search"')
        self.assertNotContains(page, "Searching")
        self.assertNotContains(page, "hx-indicator")
        self.assertContains(page, "incident-pill-edit")
        self.assertContains(page, "incident-pill-insurance")
        self.assertContains(page, "incident-pill-security")
        self.assertContains(page, "incident-pill-pdf")
        self.assertContains(page, "color: #0d6efd")
        self.assertContains(page, "color: #0f766e")
        self.assertContains(page, "color: #dc3545")
        self.assertContains(page, "color: #6c757d")
        self.assertContains(page, "gap: 0.7rem")

    def test_checklist_tabs_keep_drafts_and_submitted_lists(self):
        ChecklistTemplate.objects.create(name="Workplace Inspection")
        draft = Checklist.objects.create(
            title="Our draft",
            created_by=self.user,
            store=self.store,
            status="draft",
        )
        Checklist.objects.create(
            title="Their secret list",
            created_by=self.other_user,
            store=self.other_store,
            status="draft",
        )
        page = self.client.get(reverse("checklist_dashboard"), {"tab": "inprogress"})
        self.assertContains(page, "Our draft")
        self.assertContains(page, reverse("checklist_edit", kwargs={"slug": draft.slug}))
        self.assertContains(page, "Resume")
        self.assertNotContains(page, "Their secret list")
        self.assertContains(page, 'id="inprogress"')

        by_store = self.client.get(
            reverse("checklist_dashboard"), {"tab": "inprogress", "q": "12"}
        )
        self.assertContains(by_store, "Our draft")
        self.assertNotContains(by_store, "Their secret list")

        start = self.client.get(reverse("checklist_dashboard"))
        self.assertContains(start, "Workplace Inspection")
        self.assertContains(start, 'id="start"')
        self.assertContains(start, "show active")

        for index in range(CHECKLIST_PAGE_SIZE + 1):
            Checklist.objects.create(
                title="Sent %02d" % index,
                created_by=self.user,
                store=self.store,
                status="submitted",
                submitted_by=self.user,
                submitted_at=timezone.now(),
            )
        paged = self.client.get(
            reverse("checklist_dashboard"), {"tab": "submitted", "page": 2}
        )
        self.assertContains(paged, "Page 2 of 2")
        self.assertContains(paged, "record-pager")
        self.assertContains(paged, "tab=submitted&page=1")
        self.assertContains(paged, "View")
