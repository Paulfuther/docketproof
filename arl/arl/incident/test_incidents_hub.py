from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from arl.incident.models import Incident
from arl.incident.views import INCIDENT_PAGE_SIZE
from arl.user.models import CustomUser, Employer, Store


def _grant(user, *codenames):
    content_type = ContentType.objects.get_for_model(Incident)
    perms = Permission.objects.filter(
        content_type=content_type, codename__in=codenames
    )
    user.user_permissions.add(*list(perms))


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class IncidentHubTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Petro Test")
        self.other_employer = Employer.objects.create(name="Other Co")
        self.user = CustomUser.objects.create_user(
            username="incident-gsa",
            email="incident-gsa@example.com",
            password="pass12345",
            phone_number="+15196707481",
            employer=self.employer,
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
            employer=self.other_employer,
            address="99 Other St",
            city="Toronto",
            province="ON",
        )
        self.reports = patch(
            "arl.incident.views.process_new_incident_reports_task.delay"
        )
        self.reports.start()
        self.addCleanup(self.reports.stop)
        _grant(self.user, "add_incident", "view_incident")
        self.client.force_login(self.user)

    def _incident(self, description, store=None, employer=None, when=None):
        return Incident.objects.create(
            store=store or self.store,
            user_employer=employer or self.employer,
            brief_description=description,
            eventdetails="Details",
            actionstaken="Cleaned up",
            eventdate=when or date(2026, 3, 1),
        )

    def test_anonymous_user_is_sent_to_login(self):
        self.client.logout()
        response = self.client.get(reverse("incidents"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_hub_has_create_and_edit_tabs_and_one_nav_link(self):
        self._incident("Fuel spill")
        response = self.client.get(reverse("incidents"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('href="#create"', body)
        self.assertIn('href="#edit"', body)
        self.assertIn("Create site incident", body)
        self.assertIn("Edit <br>", body)
        self.assertIn("Fuel spill", body)
        self.assertIn("table table-sm align-middle", body)
        self.assertNotIn("Make Incident Form", body)
        self.assertNotIn("Edit Incident Form", body)
        self.assertNotIn("DataTable(", body)
        self.assertNotIn('id="table_id"', body)
        self.assertEqual(body.count('href="%s"' % reverse("incidents")), 1)

    def test_edit_tab_lists_only_this_employer_and_links_to_the_form(self):
        own = self._incident("Fuel spill")
        self._incident(
            "Other site",
            store=self.other_store,
            employer=self.other_employer,
        )
        response = self.client.get(reverse("incidents") + "?tab=edit")
        self.assertContains(response, "Fuel spill")
        self.assertNotContains(response, "Other site")
        self.assertContains(
            response, reverse("update_incident", kwargs={"pk": own.pk})
        )
        self.assertContains(response, "Insurance PDF")
        self.assertContains(response, "Security PDF")
        self.assertNotContains(response, "Investigation PDF")

    def test_investigation_pdf_is_offered_to_the_restricted_group(self):
        self._incident("Fuel spill")
        group = Group.objects.create(name="abm_incident_pdf")
        self.user.groups.add(group)
        response = self.client.get(reverse("incidents") + "?tab=edit")
        self.assertContains(response, "Investigation PDF")

    def test_edit_tab_search_uses_a_plain_query(self):
        self._incident("Fuel spill", when=date(2026, 3, 2))
        self._incident("Slip near door", when=date(2026, 3, 1))
        response = self.client.get(reverse("incidents"), {"tab": "edit", "q": "Fuel"})
        self.assertContains(response, "Fuel spill")
        self.assertNotContains(response, "Slip near door")
        by_store = self.client.get(
            reverse("incidents"), {"tab": "edit", "q": "12"}
        )
        self.assertContains(by_store, "Fuel spill")

    def test_edit_tab_paginates_without_a_table_plugin(self):
        for index in range(INCIDENT_PAGE_SIZE + 1):
            self._incident("Incident %02d" % index, when=date(2026, 1, 1))
        response = self.client.get(reverse("incidents"), {"tab": "edit", "page": 2})
        self.assertContains(response, "Page 2 of 2")
        self.assertNotContains(response, "DataTable(")

    def test_legacy_create_and_list_urls_open_the_hub(self):
        create = self.client.get(reverse("create_incident"))
        self.assertRedirects(create, reverse("incidents") + "?tab=create")
        listing = self.client.get(reverse("incident_list"))
        self.assertRedirects(listing, reverse("incidents") + "?tab=edit")

    def test_invalid_create_post_rerenders_the_create_tab(self):
        response = self.client.post(reverse("create_incident"), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create site incident")
        self.assertContains(response, 'id="create"')
        self.assertContains(response, "show active")

    def test_user_without_incident_permission_is_denied(self):
        self.user.user_permissions.clear()
        response = self.client.get(reverse("incidents"))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Permission Denied", status_code=403)

    def test_view_only_user_sees_the_edit_table_and_not_the_create_form(self):
        self.user.user_permissions.clear()
        _grant(self.user, "view_incident")
        self._incident("Fuel spill")
        response = self.client.get(reverse("incidents"))
        body = response.content.decode()
        self.assertIn("Fuel spill", body)
        self.assertIn('href="#edit"', body)
        self.assertNotIn('href="#create"', body)
        self.assertNotIn('id="my-dropzone"', body)

    def test_edit_form_is_a_plain_page_back_to_the_hub(self):
        incident = self._incident("Fuel spill")
        response = self.client.get(
            reverse("update_incident", kwargs={"pk": incident.pk})
        )
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Edit site incident", body)
        self.assertIn(reverse("incidents") + "?tab=edit", body)
        self.assertIn("Section 1: Incident Information", body)
        self.assertNotIn("DataTable(", body)
