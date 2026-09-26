import csv
from datetime import timedelta
from io import StringIO

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arl.documentflow.models import ImmigrationStatusEvent
from arl.documentflow.services_immigration import (
    AUTHORIZED_EXTENSION_LABEL,
    build_immigration_audit,
    immigration_audit_export_records,
)
from arl.dsign.models import SignedDocumentFile
from arl.user.models import CustomUser, Employer, Store
from arl.user.services import set_user_sin

TEMPORARY_SIN = "946454286"
PERMANENT_SIN = "130692544"
FERNET_TEST_KEY = "iNKsr5HSwIPaWnIPgLRQLSluZRH0zEzZnJ_f6WdFZBQ="
SIN_SETTINGS = dict(
    SECRET_KEY="ci-test-secret-key-not-for-production",
    FERNET_PRIMARY_KEY=FERNET_TEST_KEY,
    FERNET_OLD_KEYS=[],
    SIN_HASH_SALT="work-permit-test-salt",
)


@override_settings(**SIN_SETTINGS)
class ImmigrationAuditExportTests(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.employer = Employer.objects.create(name="Audit Fuels")
        self.other = Employer.objects.create(name="Other Fuels")
        self.store = Store.objects.create(
            number=42,
            employer=self.employer,
            address="1 Main",
            city="London",
            province="ON",
        )
        self.hr = self._person(
            "hr.audit",
            "+15195559001",
            None,
            None,
            employer=self.employer,
            first_name="Hannah",
            last_name="Audit",
        )
        manager, _ = Group.objects.get_or_create(name="Manager")
        immigration, _ = Group.objects.get_or_create(name="immigration_audit")
        employer_group, _ = Group.objects.get_or_create(name="EMPLOYER")
        self.manager_group = manager
        self.immigration_group = immigration
        self.employer_group = employer_group
        self.hr.groups.add(manager, immigration)

    def _person(self, username, phone, sin, permit_days, employer=None, **extra):
        sin_days = extra.pop("sin_days", None)
        is_active = extra.pop("is_active", True)
        store = extra.pop("store", None)
        user = CustomUser.objects.create_user(
            username=username,
            email=extra.pop("email", f"{username}@example.com"),
            password="pass12345",
            phone_number=phone,
            employer=employer or self.employer,
            first_name=extra.pop("first_name", "First"),
            last_name=extra.pop("last_name", "Last"),
            is_active=is_active,
            **extra,
        )
        if store is not None:
            user.store = store
            user.save(update_fields=["store"])
        if sin:
            set_user_sin(user, sin, validate_luhn=False, save=True)
        updates = []
        if permit_days is not None:
            user.work_permit_expiration_date = self.today + timedelta(days=permit_days)
            updates.append("work_permit_expiration_date")
        if sin_days is not None:
            user.sin_expiration_date = self.today + timedelta(days=sin_days)
            updates.append("sin_expiration_date")
        if updates:
            user.save(update_fields=updates)
        return user

    def _csv_rows(self, user=None, query=""):
        self.client.force_login(user or self.hr)
        url = reverse("immigration_audit_export")
        if query:
            url = f"{url}?{query}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(response.content.startswith(b"\xef\xbb\xbf"))
        text = response.content.decode("utf-8-sig")
        return response, list(csv.DictReader(StringIO(text)))

    def test_export_matches_audit_ranking_and_hides_other_people(self):
        urgent = self._person(
            "una.urgent",
            "+15195559010",
            TEMPORARY_SIN,
            -4,
            sin_days=400,
            store=self.store,
            first_name="Una",
            last_name="Urgent",
        )
        authorized = self._person(
            "ada.auth",
            "+15195559011",
            TEMPORARY_SIN,
            15,
            sin_days=20,
            store=self.store,
            first_name="Ada",
            last_name="Lovelace, Jr",
            work_permit_extension_requested=True,
            work_permit_extension_date=self.today - timedelta(days=1),
        )
        event_person = self._person(
            "ned.permit",
            "+15195559012",
            TEMPORARY_SIN,
            200,
            sin_days=400,
            first_name="Ned",
            last_name="Newpermit",
        )
        ImmigrationStatusEvent.objects.create(
            user=event_person,
            employer=self.employer,
            status_type="new_work_permit",
            effective_date=self.today - timedelta(days=8),
            reference_number="WP-88",
        )
        missing = self._person(
            "mia.missing",
            "+15195559013",
            None,
            None,
            first_name="Mia",
            last_name="Missing",
        )
        permanent = self._person(
            "pat.perm",
            "+15195559014",
            PERMANENT_SIN,
            -30,
            store=self.store,
            first_name="Pat",
            last_name="Permanent",
        )
        self._person(
            "ina.gone",
            "+15195559015",
            TEMPORARY_SIN,
            -1,
            is_active=False,
            email="inactive-secret@example.com",
            first_name="Ina",
            last_name="Gone",
        )
        self._person(
            "oth.er",
            "+15195559016",
            TEMPORARY_SIN,
            -1,
            employer=self.other,
            email="other-employer@example.com",
            first_name="Otto",
            last_name="Other",
        )
        document = SignedDocumentFile.objects.create(
            user=authorized,
            employer=self.employer,
            envelope_id="env-auth",
            file_name="extension.pdf",
            file_path="DOCUMENTS/extension.pdf",
        )
        ImmigrationStatusEvent.objects.create(
            user=authorized,
            employer=self.employer,
            status_type="work_permit_extension",
            effective_date=self.today - timedelta(days=1),
            reference_number="IRCC-55",
            document_file=document,
        )

        audit_names = [
            (row["employee"].get_full_name() or "").strip()
            for row in build_immigration_audit(self.employer)["immigration_rows"]
        ]
        response, rows = self._csv_rows(query="imm_q=Una&imm_flagged=1")
        self.assertIn(
            'attachment; filename="immigration-audit-audit-fuels.csv"',
            response["Content-Disposition"],
        )
        self.assertEqual([row["Name"] for row in rows], audit_names)
        exported_emails = {row["Email"] for row in rows}
        self.assertNotIn("inactive-secret@example.com", exported_emails)
        self.assertNotIn("other-employer@example.com", exported_emails)
        self.assertNotIn(TEMPORARY_SIN, response.content.decode("utf-8-sig"))
        self.assertNotIn(PERMANENT_SIN, response.content.decode("utf-8-sig"))

        by_email = {row["Email"]: row for row in rows}
        urgent_row = by_email[urgent.email]
        self.assertEqual(urgent_row["Store"], "42")
        self.assertEqual(urgent_row["Employer"], "Audit Fuels")
        self.assertEqual(urgent_row["Masked SIN"], "*** *** 4286")
        self.assertEqual(urgent_row["SIN Type"], "temporary")
        self.assertEqual(urgent_row["SIN Days Left"], "400")
        self.assertEqual(urgent_row["Permit Days Left"], "-4")
        self.assertEqual(
            urgent_row["Permit Expiry"],
            (self.today + timedelta(days=-4)).isoformat(),
        )
        self.assertEqual(urgent_row["Extension Authorized"], "No")
        self.assertEqual(urgent_row["Overall Status"], "Urgent")
        self.assertEqual(urgent_row["Overall Status Code"], "urgent")
        self.assertEqual(urgent_row["Latest Event Type"], "")
        self.assertEqual(urgent_row["Latest Event Has Document"], "No")
        self.assertEqual(urgent_row["Latest Event Has Reference"], "No")

        auth_row = by_email[authorized.email]
        self.assertEqual(auth_row["Name"], "Ada Lovelace, Jr")
        self.assertEqual(auth_row["SIN Type"], "temporary")
        self.assertEqual(auth_row["Extension Authorized"], "Yes")
        self.assertEqual(auth_row["Overall Status"], AUTHORIZED_EXTENSION_LABEL)
        self.assertEqual(auth_row["Overall Status Code"], "extension_pending")
        self.assertEqual(auth_row["Latest Event Type"], "Work Permit Extension")
        self.assertEqual(
            auth_row["Latest Event Effective Date"],
            (self.today - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(auth_row["Latest Event Has Document"], "Yes")
        self.assertEqual(auth_row["Latest Event Has Reference"], "Yes")
        self.assertNotIn("Extension Pending", response.content.decode("utf-8-sig"))

        new_row = by_email[event_person.email]
        self.assertEqual(new_row["Extension Authorized"], "Yes")
        self.assertEqual(new_row["Overall Status"], AUTHORIZED_EXTENSION_LABEL)
        self.assertEqual(new_row["Latest Event Type"], "New Work Permit")
        self.assertEqual(new_row["Latest Event Has Document"], "No")
        self.assertEqual(new_row["Latest Event Has Reference"], "Yes")

        missing_row = by_email[missing.email]
        self.assertEqual(missing_row["SIN Type"], "missing")
        self.assertEqual(missing_row["Masked SIN"], "*********")
        self.assertEqual(missing_row["SIN Expiry"], "")
        self.assertEqual(missing_row["SIN Days Left"], "")
        self.assertEqual(missing_row["Extension Authorized"], "No")

        permanent_row = by_email[permanent.email]
        self.assertEqual(permanent_row["SIN Type"], "permanent")
        self.assertEqual(permanent_row["Masked SIN"], "*** *** 2544")
        self.assertEqual(permanent_row["Overall Status"], "Compliant")
        self.assertEqual(permanent_row["Overall Status Code"], "compliant")
        self.assertEqual(permanent_row["Extension Authorized"], "No")

        records = immigration_audit_export_records(self.employer)
        self.assertEqual(
            [record["email"] for record in records],
            [row["Email"] for row in rows],
        )

    def test_audit_page_shows_new_label_and_download(self):
        self._person(
            "lee.letter",
            "+15195559020",
            TEMPORARY_SIN,
            10,
            first_name="Lee",
            last_name="Letter",
            work_permit_extension_requested=True,
            work_permit_extension_date=self.today,
        )
        self.client.force_login(self.hr)
        response = self.client.get(reverse("immigration_audit_partial"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, AUTHORIZED_EXTENSION_LABEL)
        self.assertContains(response, "Download spreadsheet")
        self.assertContains(response, reverse("immigration_audit_export"))
        self.assertNotContains(response, "Extension Pending")
        self.assertNotContains(response, "Authorized - extension on file")

    def test_export_requires_hr_and_immigration_access(self):
        url = reverse("immigration_audit_export")
        anonymous = self.client.get(url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login/", anonymous["Location"])

        staff = self._person(
            "sam.staff",
            "+15195559030",
            None,
            None,
            first_name="Sam",
            last_name="Staff",
        )
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 403)

        staff.groups.add(self.manager_group)
        self.assertEqual(self.client.get(url).status_code, 403)

        staff.groups.remove(self.manager_group)
        staff.groups.add(self.immigration_group)
        self.assertEqual(self.client.get(url).status_code, 403)

        staff.groups.add(self.employer_group)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn(staff.email, response.content.decode("utf-8-sig"))

    def test_superuser_with_employer_can_export_without_immigration_group(self):
        boss = self._person(
            "bea.boss",
            "+15195559040",
            None,
            None,
            first_name="Bea",
            last_name="Boss",
        )
        boss.is_superuser = True
        boss.save(update_fields=["is_superuser"])
        boss.groups.add(self.manager_group)
        self.client.force_login(boss)
        response = self.client.get(reverse("immigration_audit_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(boss.email, response.content.decode("utf-8-sig"))
