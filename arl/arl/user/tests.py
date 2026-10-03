from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from twilio.base.exceptions import TwilioException
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse

from arl.documentflow.models import ImmigrationStatusEvent, SentDocuSignEnvelope
from arl.documentflow.services_immigration import build_immigration_audit
from arl.dsign.models import SignedDocumentFile
from arl.quiz.models import Checklist, ChecklistTemplate, ChecklistTemplateItem
from arl.user.models import EmployeeDocument, Employer, Store
from arl.user.views import handle_new_hire_registration

User = get_user_model()
PASSWORD = "Dock3t-Proof-Employee"


class EmployeeTestCase(TestCase):
    def setUp(self):
        post_save.disconnect(handle_new_hire_registration, sender=User)
        self.employer = Employer.objects.create(name="Acme Retail", is_active=True)
        self.other_employer = Employer.objects.create(name="Other Co", is_active=True)
        self.employee = self._user(
            "ada",
            "Ada",
            "Lovelace",
            "+14161234567",
            self.employer,
        )
        self.coworker = self._user(
            "ben",
            "Ben",
            "Wright",
            "+14161234568",
            self.employer,
        )
        self.staff = self._user(
            "pat",
            "Pat",
            "Staff",
            "+14161234569",
            self.employer,
            is_staff=True,
        )

    def _user(self, username, first, last, phone, employer, is_staff=False):
        return User.objects.create_user(
            username=username,
            password=PASSWORD,
            email=f"{username}@example.com",
            first_name=first,
            last_name=last,
            phone_number=phone,
            employer=employer,
            is_staff=is_staff,
        )

    def _login(self, username):
        return self.client.post(
            reverse("login"),
            {"username": username, "password": PASSWORD},
        )


class EmployeeLoginAccessTests(EmployeeTestCase):
    def _assert_password_does_not_start_session(self, response, user):
        self.assertRedirects(
            response, reverse("verification_page"), fetch_redirect_response=False
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session["user_id"], user.id)
        blocked = self.client.get(reverse("employee_home"))
        self.assertEqual(blocked.status_code, 302)
        self.assertIn("/login/", blocked.url)

    @patch("arl.user.views.request_verification_token")
    def test_employee_password_does_not_start_a_session(self, send_code):
        response = self._login("ada")
        send_code.assert_called_once()
        self._assert_password_does_not_start_session(response, self.employee)

    @patch("arl.user.views.request_verification_token")
    def test_manager_password_does_not_start_a_session(self, send_code):
        manager_group = Group.objects.create(name="Manager")
        self.employee.groups.add(manager_group)
        self.assertFalse(self.employee.is_employee_account)

        response = self._login("ada")
        send_code.assert_called_once()
        self._assert_password_does_not_start_session(response, self.employee)

    @patch("arl.user.views.check_verification_token", return_value=False)
    @patch("arl.user.views.request_verification_token")
    def test_wrong_phone_code_does_not_start_a_session(self, send_code, check_code):
        self._login("ada")
        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "000000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        check_code.assert_called_once()

    @patch("arl.user.views.check_verification_token", return_value=True)
    @patch("arl.user.views.request_verification_token")
    def test_employee_session_starts_only_after_phone_code(self, send_code, check_code):
        self._login("ada")
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "123456"},
        )
        self.assertRedirects(
            response, reverse("employee_home"), fetch_redirect_response=False
        )
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.employee.id)
        self.assertGreater(self.client.session.get_expiry_age(), 60 * 60 * 24 * 13)
        check_code.assert_called_once()

        home = self.client.get(reverse("employee_home"))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Signed")

    @patch("arl.user.views.check_verification_token", return_value=True)
    @patch("arl.user.views.request_verification_token")
    def test_manager_session_starts_only_after_phone_code(self, send_code, check_code):
        manager_group = Group.objects.create(name="Manager")
        self.employee.groups.add(manager_group)

        self._login("ada")
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "123456"},
        )
        self.assertRedirects(
            response, reverse("manager_employee_list"), fetch_redirect_response=False
        )
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.employee.id)
        check_code.assert_called_once()

        roster = self.client.get(reverse("manager_employee_list"))
        self.assertContains(roster, "Ben Wright")
        self.assertContains(roster, "Pat Staff")
        self.assertContains(roster, "Ada Lovelace")
        self.assertNotContains(roster, "Other Co")

    @patch("arl.user.views.request_verification_token", side_effect=TwilioException("down"))
    def test_phone_code_failure_does_not_start_a_session(self, send_code):
        response = self._login("ada")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertNotIn("user_id", self.client.session)
        send_code.assert_called_once()

    @patch("arl.user.views.request_verification_token")
    def test_bad_password_and_inactive_user_do_not_get_a_session(self, send_code):
        bad = self.client.post(
            reverse("login"),
            {"username": "ada", "password": "wrong-password"},
        )
        self.assertEqual(bad.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(send_code.called)

        self.employee.is_active = False
        self.employee.save(update_fields=["is_active"])
        inactive = self._login("ada")
        self.assertEqual(inactive.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    @patch("arl.user.views.request_verification_token")
    def test_staff_login_does_not_create_a_session_before_verification(self, send_code):
        response = self._login("pat")

        send_code.assert_called_once()
        self.assertRedirects(
            response, reverse("verification_page"), fetch_redirect_response=False
        )
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session["user_id"], self.staff.id)

    @patch("arl.user.views.check_verification_token", return_value=True)
    @patch("arl.user.views.request_verification_token")
    def test_staff_session_starts_only_after_phone_code(self, send_code, check_code):
        self._login("pat")
        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "123456"},
        )
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.staff.id)
        check_code.assert_called_once()
        roster = self.client.get(reverse("manager_employee_list"))
        self.assertEqual(roster.status_code, 403)

    def test_anonymous_users_cannot_open_employee_pages(self):
        urls = [
            reverse("employee_home"),
            reverse("employee_immigration_upload"),
            reverse("checklist_dashboard"),
            reverse("manager_employee_list"),
            reverse("manager_employee_detail", args=[self.employee.id]),
        ]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/login/", response.url)

    def test_employee_cannot_open_hr_dashboard(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("hr_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/home/", response.url)

    def test_employee_sees_signed_docs_and_opens_own_unsigned_pill(self):
        SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="signed001",
            file_name="offer.pdf",
            file_path="DOCUMENTS/acme/offer.pdf",
            document_title="Signed offer",
        )
        own = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="Handbook",
            envelope_id="env-handbook",
            status="sent",
        )
        other = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.coworker,
            template_name="Coworker secret",
            envelope_id="env-secret",
            status="sent",
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="Already signed policy",
            envelope_id="env-done",
            status="completed",
        )

        self.client.force_login(self.employee)
        home = self.client.get(reverse("employee_home"))
        self.assertContains(home, "Signed offer")
        self.assertContains(home, "Handbook")
        self.assertNotContains(home, "Coworker secret")
        self.assertNotContains(home, "Already signed policy")
        self.assertContains(home, reverse("checklist_dashboard"))
        self.assertContains(home, reverse("employee_immigration_upload"))

        opened = self.client.get(
            reverse("employee_unsigned_document", args=[own.id])
        )
        self.assertEqual(opened.status_code, 200)
        self.assertContains(opened, "Handbook")
        self.assertContains(opened, "env-handbook")
        self.assertNotContains(opened, "Coworker secret")

        denied = self.client.get(
            reverse("employee_unsigned_document", args=[other.id])
        )
        self.assertEqual(denied.status_code, 403)
        self.assertNotContains(denied, "Coworker secret", status_code=403)

    @patch(
        "arl.dsign.helpers.get_recipient_view_url",
        return_value="https://sign.example/handbook",
    )
    def test_opening_an_unsigned_document_uses_that_envelope(self, get_url):
        own = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="Handbook",
            envelope_id="env-handbook",
            status="delivered",
        )
        self.client.force_login(self.employee)
        response = self.client.post(
            reverse("employee_open_unsigned_document", args=[own.id])
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "https://sign.example/handbook")
        get_url.assert_called_once()
        self.assertEqual(get_url.call_args.kwargs["envelope_id"], "env-handbook")

    def test_employee_can_start_the_existing_checklist(self):
        store = Store.objects.create(number=10, employer=self.employer)
        template = ChecklistTemplate.objects.create(
            name="Opening checklist", is_active=True
        )
        ChecklistTemplateItem.objects.create(
            template=template, text="Doors locked", order=1
        )
        self.client.force_login(self.employee)

        dashboard = self.client.get(reverse("checklist_dashboard"))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, "Opening checklist")

        started = self.client.post(
            reverse("checklist_from_template", args=[template.id]),
            {"title": "Opening checklist", "notes": "", "store": store.id},
        )
        checklist = Checklist.objects.get(created_by=self.employee)
        self.assertEqual(checklist.status, "draft")
        self.assertRedirects(
            started,
            reverse("checklist_edit", args=[checklist.slug]),
            fetch_redirect_response=False,
        )
        edit = self.client.get(reverse("checklist_edit", args=[checklist.slug]))
        self.assertEqual(edit.status_code, 200)
        self.assertContains(edit, "Doors locked")

    @patch("arl.user.views.download_from_s3", return_value=HttpResponse("file-bytes"))
    def test_download_is_limited_to_the_owner_or_same_company_manager(self, download):
        own = SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="own-doc",
            file_name="own.pdf",
            file_path="DOCUMENTS/acme/own.pdf",
            document_title="Own file",
        )
        theirs = SignedDocumentFile.objects.create(
            user=self.coworker,
            employer=self.employer,
            envelope_id="their-doc",
            file_name="their.pdf",
            file_path="DOCUMENTS/acme/their.pdf",
            document_title="Their file",
        )

        self.client.force_login(self.employee)
        allowed = self.client.get(reverse("download_signed_document", args=[own.id]))
        self.assertEqual(allowed.status_code, 200)
        denied = self.client.get(reverse("download_signed_document", args=[theirs.id]))
        self.assertEqual(denied.status_code, 403)

        self.client.logout()
        anonymous = self.client.get(reverse("download_signed_document", args=[own.id]))
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/login/", anonymous.url)

        manager_group = Group.objects.create(name="Manager")
        self.staff.is_staff = False
        self.staff.save(update_fields=["is_staff"])
        self.staff.groups.add(manager_group)
        self.client.force_login(self.staff)
        managed = self.client.get(reverse("download_signed_document", args=[theirs.id]))
        self.assertEqual(managed.status_code, 200)
        self.assertGreaterEqual(download.call_count, 2)


class ManagerEmployeeListTests(EmployeeTestCase):
    def setUp(self):
        super().setUp()
        self.manager_group = Group.objects.create(name="Manager")
        self.staff.is_staff = False
        self.staff.save(update_fields=["is_staff"])
        self.staff.groups.add(self.manager_group)
        self.outsider = self._user(
            "out",
            "Out",
            "Sider",
            "+14161234570",
            self.other_employer,
        )
        self.coworker.sin = "987654321"
        self.coworker.work_permit_expiration_date = date(2027, 3, 1)
        self.coworker.work_permit_extension_requested = True
        self.coworker.work_permit_extension_date = date(2026, 3, 1)
        self.coworker.save(
            update_fields=[
                "sin",
                "work_permit_expiration_date",
                "work_permit_extension_requested",
                "work_permit_extension_date",
            ]
        )

    def test_manager_sees_only_their_company(self):
        inactive = self._user(
            "old", "Old", "Hand", "+14161234571", self.employer
        )
        inactive.is_active = False
        inactive.save(update_fields=["is_active"])

        self.client.force_login(self.staff)
        roster = self.client.get(reverse("manager_employee_list"))
        self.assertEqual(roster.status_code, 200)
        self.assertContains(roster, "Ada Lovelace")
        self.assertContains(roster, "Ben Wright")
        self.assertContains(roster, "Pat Staff")
        self.assertContains(
            roster, reverse("manager_employee_detail", args=[self.coworker.id])
        )
        self.assertNotContains(roster, "Out Sider")
        self.assertNotContains(roster, "Old Hand")

        home = self.client.get(reverse("home"))
        self.assertRedirects(home, reverse("manager_employee_list"))
        own_docs = self.client.get(reverse("employee_home"))
        self.assertRedirects(own_docs, reverse("manager_employee_list"))

    def test_detail_shows_signed_docs_unsigned_pills_and_immigration(self):
        SignedDocumentFile.objects.create(
            user=self.coworker,
            employer=self.employer,
            envelope_id="signed-ben",
            file_name="offer.pdf",
            file_path="DOCUMENTS/acme/offer.pdf",
            document_title="Signed offer",
        )
        envelope = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.coworker,
            template_name="Handbook",
            envelope_id="env-handbook",
            status="sent",
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.coworker,
            template_name="Already signed policy",
            envelope_id="env-done",
            status="completed",
        )
        ImmigrationStatusEvent.objects.create(
            user=self.coworker,
            employer=self.employer,
            status_type="maintained_status",
            effective_date=date(2026, 3, 1),
            expiry_date=date(2027, 3, 1),
            reference_number="IRCC-42",
        )

        self.client.force_login(self.staff)
        page = self.client.get(
            reverse("manager_employee_detail", args=[self.coworker.id])
        )
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Signed offer")
        self.assertContains(page, "Handbook")
        self.assertContains(page, "Sent")
        self.assertNotContains(page, "Already signed policy")
        self.assertContains(page, "Maintained Status")
        self.assertContains(page, "IRCC-42")
        self.assertContains(page, "Extension requested")
        self.assertContains(page, "March 1, 2027")
        self.assertContains(
            page, reverse("download_signed_document", args=[SignedDocumentFile.objects.get().id])
        )
        self.assertNotContains(page, "Open this document")
        self.assertNotContains(
            page, reverse("employee_unsigned_document", args=[envelope.id])
        )
        self.assertNotContains(page, "987654321")

    def test_manager_cannot_open_another_companys_employee(self):
        other_manager = self._user(
            "mia",
            "Mia",
            "Chen",
            "+14161234572",
            self.other_employer,
        )
        other_manager.groups.add(self.manager_group)

        self.client.force_login(other_manager)
        roster = self.client.get(reverse("manager_employee_list"))
        self.assertContains(roster, "Out Sider")
        self.assertNotContains(roster, "Ada Lovelace")
        self.assertNotContains(roster, "Ben Wright")
        self.assertNotContains(roster, "Pat Staff")

        denied = self.client.get(
            reverse("manager_employee_detail", args=[self.coworker.id])
        )
        self.assertEqual(denied.status_code, 403)
        self.assertNotContains(denied, "Ben Wright", status_code=403)
        self.assertNotContains(denied, "Handbook", status_code=403)

    def test_employee_cannot_open_the_manager_list(self):
        self.client.force_login(self.employee)
        roster = self.client.get(reverse("manager_employee_list"))
        self.assertEqual(roster.status_code, 403)
        detail = self.client.get(
            reverse("manager_employee_detail", args=[self.coworker.id])
        )
        self.assertEqual(detail.status_code, 403)

        home = self.client.get(reverse("employee_home"))
        self.assertEqual(home.status_code, 200)
        self.assertNotContains(home, "Ben Wright")

    def test_missing_employee_is_not_found(self):
        self.client.force_login(self.staff)
        missing = self.client.get(reverse("manager_employee_detail", args=[999999]))
        self.assertEqual(missing.status_code, 404)

    def test_employer_group_sees_the_same_company_list(self):
        employer_group = Group.objects.create(name="EMPLOYER")
        self.employee.groups.add(employer_group)
        self.assertTrue(self.employee.is_company_manager)
        self.assertFalse(self.employee.is_employee_account)

        self.client.force_login(self.employee)
        roster = self.client.get(reverse("manager_employee_list"))
        self.assertEqual(roster.status_code, 200)
        self.assertContains(roster, "Ben Wright")
        self.assertNotContains(roster, "Out Sider")


class EmployeeImmigrationUploadTests(EmployeeTestCase):
    def _upload(self, **overrides):
        data = {
            "document_title": "Work permit extension",
            "immigration_status_type": "work_permit_extension",
            "immigration_effective_date": "2026-03-01",
            "immigration_expiry_date": "2027-03-01",
            "immigration_reference_number": "IRCC-42",
            "notes": "Submitted online",
            "user_id": str(self.coworker.id),
        }
        data.update(overrides)
        upload = data.pop("file", None)
        if upload is None:
            upload = SimpleUploadedFile(
                "permit.pdf", b"%PDF-1.4 permit", content_type="application/pdf"
            )
        data["file"] = upload
        return self.client.post(reverse("employee_immigration_upload"), data)

    @patch("arl.bucket.helpers.upload_to_linode_object_storage")
    def test_upload_updates_the_existing_immigration_tracker(self, store_file):
        self.client.force_login(self.employee)
        response = self._upload()

        self.assertRedirects(response, reverse("employee_immigration_upload"))
        store_file.assert_called_once()

        event = ImmigrationStatusEvent.objects.get(user=self.employee)
        self.assertEqual(event.status_type, "work_permit_extension")
        self.assertEqual(event.effective_date, date(2026, 3, 1))
        self.assertEqual(event.expiry_date, date(2027, 3, 1))
        self.assertEqual(event.reference_number, "IRCC-42")
        self.assertEqual(event.notes, "Submitted online")
        self.assertEqual(event.employer_id, self.employer.id)
        self.assertEqual(event.created_by_id, self.employee.id)
        self.assertTrue(event.is_active)
        self.assertIsNotNone(event.document_file_id)
        self.assertEqual(event.document_file.user_id, self.employee.id)
        self.assertEqual(event.document_file.document_title, "Work permit extension")
        self.assertFalse(event.document_file.is_company_document)

        self.employee.refresh_from_db()
        self.assertTrue(self.employee.work_permit_extension_requested)
        self.assertEqual(self.employee.work_permit_extension_date, date(2026, 3, 1))
        self.assertEqual(self.employee.work_permit_expiration_date, date(2027, 3, 1))

        self.coworker.refresh_from_db()
        self.assertFalse(self.coworker.work_permit_extension_requested)
        self.assertFalse(
            ImmigrationStatusEvent.objects.filter(user=self.coworker).exists()
        )
        self.assertEqual(EmployeeDocument.objects.count(), 0)

        audit = build_immigration_audit(self.employer)
        row = next(
            item for item in audit["immigration_rows"] if item["employee"].id == self.employee.id
        )
        self.assertEqual(row["latest_immigration_event"].id, event.id)
        self.assertEqual(row["extension_requested"], True)
        self.assertEqual(row["permit_expiry"], date(2027, 3, 1))

        follow = self.client.get(reverse("employee_immigration_upload"))
        self.assertContains(follow, "Work Permit Extension")
        self.assertContains(follow, "IRCC-42")

    @patch("arl.bucket.helpers.upload_to_linode_object_storage")
    def test_new_work_permit_clears_the_extension_flag(self, store_file):
        self.employee.work_permit_extension_requested = True
        self.employee.work_permit_extension_date = date(2026, 1, 1)
        self.employee.save(
            update_fields=[
                "work_permit_extension_requested",
                "work_permit_extension_date",
            ]
        )
        self.client.force_login(self.employee)
        response = self._upload(
            document_title="New permit",
            immigration_status_type="new_work_permit",
            immigration_effective_date="2026-06-01",
            immigration_expiry_date="2028-06-01",
            immigration_reference_number="WP-9",
        )
        self.assertEqual(response.status_code, 302)
        store_file.assert_called_once()

        self.employee.refresh_from_db()
        self.assertFalse(self.employee.work_permit_extension_requested)
        self.assertEqual(self.employee.work_permit_expiration_date, date(2028, 6, 1))
        event = ImmigrationStatusEvent.objects.get(user=self.employee)
        self.assertEqual(event.status_type, "new_work_permit")
        self.assertEqual(event.document_file.document_title, "New permit")

    def test_invalid_upload_does_not_change_the_tracker(self):
        self.client.force_login(self.employee)
        response = self._upload(
            immigration_status_type="not-a-status",
            file=SimpleUploadedFile(
                "permit.pdf", b"%PDF-1.4 permit", content_type="application/pdf"
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ImmigrationStatusEvent.objects.exists())
        self.assertFalse(SignedDocumentFile.objects.exists())
        self.employee.refresh_from_db()
        self.assertFalse(self.employee.work_permit_extension_requested)

    def test_anonymous_upload_is_rejected(self):
        response = self._upload()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)
        self.assertFalse(ImmigrationStatusEvent.objects.exists())
