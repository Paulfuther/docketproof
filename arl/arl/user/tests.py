from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from twilio.base.exceptions import TwilioException
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save
from django.http import HttpResponse
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from arl.documentflow.models import (
    ImmigrationStatusEvent,
    SentDocuSignEnvelope,
    SentDocuSignRecipient,
)
from arl.documentflow.services_immigration import (
    IMMIGRATION_UPLOAD_EMAIL_GROUP,
    build_immigration_audit,
    build_immigration_tracker,
    immigration_upload_recipient_emails,
    resolve_immigration_email_group_name,
    send_immigration_upload_notification,
)
from arl.user.services import set_user_sin
from arl.dsign.models import DocuSignTemplate, SignedDocumentFile
from arl.quiz.models import Checklist, ChecklistTemplate, ChecklistTemplateItem, SaltLog
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
        gsa = Group.objects.create(name="GSA")
        self.employee.groups.add(gsa)
        self.coworker.groups.add(gsa)
        self.assertTrue(self.employee.is_employee_account)

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
        self.assertTrue(self.employee.groups.filter(name="GSA").exists())
        self.assertFalse(self.employee.is_employee_account)

        self._login("ada")
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "123456"},
        )
        self._assert_old_home_after_phone_code(response, self.employee)
        check_code.assert_called_once()
        home = self.client.get(reverse("home"))
        self.assertNotContains(home, "nav-link employee-section")
        self.assertContains(home, "staff-app")
        self.assertContains(home, "gsa-bottom-tabs")
        self.assertNotContains(home, "navbar-toggler")
        self.assertContains(home, reverse("hr_dashboard"))
        self.assertContains(home, reverse("documents_dashboard"))
        hr_page = self.client.get(reverse("hr_dashboard"))
        self.assertEqual(hr_page.status_code, 200)
        self.assertNotContains(hr_page, 'id="hrNav"')
        self.assertContains(hr_page, 'id="hrNavMobile"')
        docs_page = self.client.get(reverse("documents_dashboard"))
        self.assertNotContains(docs_page, 'id="docNav"')
        self.assertContains(docs_page, 'id="docNavMobile"')
        self.assertEqual(docs_page.status_code, 200)
        self.assertEqual(self.client.get(reverse("hr_document_view")).status_code, 200)
        checklist = self.client.get(reverse("checklist_dashboard"))
        self.assertContains(checklist, "col-lg-6")
        self.assertNotContains(checklist, "Back to documents")
        self.assertNotContains(checklist, "employee-page")

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
        self._assert_old_home_after_phone_code(response, self.staff)
        check_code.assert_called_once()

    @patch("arl.user.views.check_verification_token", return_value=True)
    @patch("arl.user.views.request_verification_token")
    def test_employer_lands_on_the_old_home_after_phone_code(self, send_code, check_code):
        employer_group = Group.objects.create(name="EMPLOYER")
        self.coworker.groups.add(employer_group)
        self.assertFalse(self.coworker.is_employee_account)

        self._login("ben")
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "123456"},
        )
        self._assert_old_home_after_phone_code(response, self.coworker)
        check_code.assert_called_once()
        self.assertEqual(self.client.get(reverse("hr_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("documents_dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("hr_document_view")).status_code, 200)

    @patch("arl.user.views.check_verification_token", return_value=True)
    @patch("arl.user.views.request_verification_token")
    def test_hr_lands_on_the_old_home_after_phone_code(self, send_code, check_code):
        hr_user = self._user(
            "helen",
            "Helen",
            "Reed",
            "+14161234570",
            self.employer,
        )
        hr_user.groups.add(Group.objects.create(name="HR"))
        self.assertTrue(hr_user.is_hr_account)
        self.assertFalse(hr_user.is_employee_account)

        self._login("helen")
        self.assertNotIn("_auth_user_id", self.client.session)
        response = self.client.post(
            reverse("verification_page"),
            {"verification_code": "123456"},
        )
        self._assert_old_home_after_phone_code(response, hr_user)
        check_code.assert_called_once()
        send_code.assert_called_once()
        self.assertContains(self.client.get(reverse("home")), reverse("hr_dashboard"))
        self.assertContains(
            self.client.get(reverse("home")), reverse("documents_dashboard")
        )
        self.assertEqual(self.client.get(reverse("hr_document_view")).status_code, 200)

    def _assert_old_home_after_phone_code(self, response, user):
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.id)
        home = self.client.get(reverse("home"))
        self.assertEqual(home.status_code, 200)
        self.assertNotContains(home, "Hello")
        new_page = self.client.get(reverse("employee_home"))
        self.assertRedirects(new_page, reverse("home"))

    def test_anonymous_users_cannot_open_employee_pages(self):
        urls = [
            reverse("employee_home"),
            reverse("employee_immigration_upload"),
            reverse("checklist_dashboard"),
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
        completed_at = timezone.now()
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="SECURITY REPORT",
            envelope_id="env-security",
            status="completed",
            completed_at=completed_at,
        )

        self.client.force_login(self.employee)
        home = self.client.get(reverse("employee_home"))
        self.assertContains(home, "Signed offer")
        self.assertContains(home, "Handbook")
        self.assertContains(home, "SECURITY REPORT")
        self.assertContains(home, completed_at.strftime("%B"))
        self.assertNotContains(home, "Coworker secret")
        self.assertContains(home, reverse("checklist_dashboard"))
        self.assertContains(home, reverse("employee_immigration_upload"))
        self.assertContains(home, reverse("salt_log_list"))
        self.assertContains(home, reverse("incident_dashboard"))
        self.assertNotContains(home, "employee-section-links")

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
        self.assertContains(home, "nav-link employee-section")
        self.assertContains(home, "scrollbar-gutter: stable")

    def test_document_lists_are_paginated(self):
        for index in range(11):
            SignedDocumentFile.objects.create(
                user=self.employee,
                employer=self.employer,
                envelope_id=f"signed-{index}",
                file_name=f"signed-{index}.pdf",
                file_path=f"DOCUMENTS/acme/signed-{index}.pdf",
                document_title=f"Signed item {index:02d}",
            )
            SentDocuSignEnvelope.objects.create(
                employer=self.employer,
                user=self.employee,
                template_name=f"Unsigned item {index:02d}",
                envelope_id=f"unsigned-{index}",
                status="sent",
            )
        self.client.force_login(self.employee)
        first = self.client.get(reverse("employee_home"))
        self.assertContains(first, "Signed item 10")
        self.assertContains(first, "Unsigned item 10")
        self.assertNotContains(first, "Signed item 00")
        self.assertNotContains(first, "Unsigned item 00")
        self.assertContains(first, "1 of 2")
        second = self.client.get(
            reverse("employee_home"), {"signed": 2, "unsigned": 2}
        )
        self.assertContains(second, "Signed item 00")
        self.assertContains(second, "Unsigned item 00")
        self.assertNotContains(second, "Signed item 10")
        self.assertNotContains(second, "Unsigned item 10")
        self.assertContains(second, "signed=1")
        self.assertContains(second, "unsigned=1")

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
        self.assertIn(
            f"envelope={own.pk}", get_url.call_args.kwargs["return_url"]
        )

    @patch("arl.dsign.helpers.EnvelopesApi")
    @patch("arl.dsign.helpers.create_api_client")
    @patch("arl.dsign.helpers.get_access_token")
    def test_open_uses_the_stored_gsa_recipient_not_the_user_email(
        self, token, api_client, envelopes_api
    ):
        token.return_value.access_token = "token"
        envelopes_api.return_value.create_recipient_view.return_value.url = (
            "https://sign.example/hey-there"
        )
        envelopes_api.return_value.list_recipients.return_value = SimpleNamespace(
            signers=[
                SimpleNamespace(
                    recipient_id="3",
                    role_name="GSA",
                    name="Han On Envelope",
                    email="han.on.envelope@example.com",
                    client_user_id=None,
                ),
                SimpleNamespace(
                    recipient_id="2",
                    role_name="Manager",
                    name="Pat Manager",
                    email="pat.manager@example.com",
                    client_user_id=None,
                ),
            ]
        )
        self.employee.email = "han.login@example.com"
        self.employee.first_name = "Han"
        self.employee.last_name = "Login"
        self.employee.save(update_fields=["email", "first_name", "last_name"])
        envelope = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="Hey there",
            envelope_id="4a492f0c-cafe-885f-81bc-65ed7fa203ab",
            status="sent",
        )
        SentDocuSignRecipient.objects.create(
            sent_envelope=envelope,
            recipient_id="3",
            role_name="GSA",
            name="Han On Envelope",
            email="han.on.envelope@example.com",
            routing_order=1,
            status="sent",
        )
        SentDocuSignRecipient.objects.create(
            sent_envelope=envelope,
            recipient_id="2",
            role_name="Manager",
            name="Pat Manager",
            email="pat.manager@example.com",
            routing_order=2,
            status="sent",
        )

        from arl.dsign.helpers import get_recipient_view_url

        url = get_recipient_view_url(
            self.employee,
            envelope.envelope_id,
            "https://app.example/employee/",
        )
        self.assertEqual(url, "https://sign.example/hey-there")
        view = envelopes_api.return_value.create_recipient_view.call_args.kwargs[
            "recipient_view_request"
        ]
        self.assertEqual(view.email, "han.on.envelope@example.com")
        self.assertEqual(view.user_name, "Han On Envelope")
        self.assertEqual(view.recipient_id, "3")
        self.assertEqual(view.client_user_id, "docketproof-3")
        self.assertNotEqual(view.email, self.employee.email)
        self.assertNotEqual(view.client_user_id, str(self.employee.id))
        updated = envelopes_api.return_value.update_recipients.call_args.kwargs
        self.assertEqual(updated["resend_envelope"], "false")
        updated_signer = updated["recipients"].signers[0]
        self.assertEqual(updated_signer.recipient_id, "3")
        self.assertEqual(updated_signer.email, "han.on.envelope@example.com")
        self.assertEqual(updated_signer.name, "Han On Envelope")
        self.assertEqual(updated_signer.client_user_id, "docketproof-3")
        self.assertEqual(updated_signer.embedded_recipient_start_url, "SIGN_AT_DOCUSIGN")
        self.assertEqual(len(updated["recipients"].signers), 1)

    @patch("arl.dsign.helpers.EnvelopesApi")
    @patch("arl.dsign.helpers.create_api_client")
    @patch("arl.dsign.helpers.get_access_token")
    def test_open_reuses_the_client_user_id_already_on_the_recipient(
        self, token, api_client, envelopes_api
    ):
        token.return_value.access_token = "token"
        envelopes_api.return_value.create_recipient_view.return_value.url = (
            "https://sign.example/mortgage"
        )
        envelopes_api.return_value.list_recipients.return_value = SimpleNamespace(
            signers=[
                SimpleNamespace(
                    recipient_id="1",
                    role_name="GSA",
                    name="Ada Lovelace",
                    email="ada@example.com",
                    client_user_id="already-on-envelope",
                )
            ]
        )
        envelope = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="mortgage",
            envelope_id="env-mortgage",
            status="sent",
        )
        SentDocuSignRecipient.objects.create(
            sent_envelope=envelope,
            recipient_id="1",
            role_name="GSA",
            name="Ada Lovelace",
            email="ada@example.com",
            status="sent",
        )

        from arl.dsign.helpers import get_recipient_view_url

        get_recipient_view_url(
            self.employee, envelope.envelope_id, "https://app.example/employee/"
        )
        envelopes_api.return_value.update_recipients.assert_not_called()
        view = envelopes_api.return_value.create_recipient_view.call_args.kwargs[
            "recipient_view_request"
        ]
        self.assertEqual(view.client_user_id, "already-on-envelope")
        self.assertEqual(view.email, "ada@example.com")
        self.assertEqual(view.recipient_id, "1")

    @patch("arl.dsign.helpers.EnvelopesApi")
    @patch("arl.dsign.helpers.create_api_client")
    @patch("arl.dsign.helpers.get_access_token")
    def test_in_app_envelope_without_a_stored_recipient_keeps_its_client_user_id(
        self, token, api_client, envelopes_api
    ):
        token.return_value.access_token = "token"
        envelopes_api.return_value.create_recipient_view.return_value.url = (
            "https://sign.example/in-app"
        )

        from arl.dsign.helpers import get_recipient_view_url

        get_recipient_view_url(
            self.employee, "brand-new-envelope", "https://app.example/hr/"
        )
        envelopes_api.return_value.list_recipients.assert_not_called()
        envelopes_api.return_value.update_recipients.assert_not_called()
        view = envelopes_api.return_value.create_recipient_view.call_args.kwargs[
            "recipient_view_request"
        ]
        self.assertEqual(view.client_user_id, str(self.employee.id))
        self.assertEqual(view.email, self.employee.email)

    @patch("arl.dsign.tasks.notify_hr")
    @patch("arl.dsign.tasks.fetch_and_upload_signed_documents")
    @patch("arl.dsign.tasks.EnvelopesApi")
    @patch("arl.dsign.tasks.get_access_token")
    def test_in_app_signing_return_uses_the_webhook_completion_path(
        self, token, envelopes_api, upload, notify_hr
    ):
        token.return_value.access_token = "token"
        template = DocuSignTemplate.objects.create(
            employer=self.employer,
            template_id="tpl-hey",
            template_name="Hey there",
        )
        envelope = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template=template,
            template_name="Hey there",
            envelope_id="4a492f0c-cafe-885f-81bc-65ed7fa203ab",
            status="sent",
        )
        SentDocuSignRecipient.objects.create(
            sent_envelope=envelope,
            recipient_id="3",
            role_name="GSA",
            name="Ada Lovelace",
            email="ada@example.com",
            status="sent",
        )
        envelopes_api.return_value.get_envelope.return_value = SimpleNamespace(
            status="completed",
            completed_date_time="2026-10-03T18:00:00Z",
        )
        envelopes_api.return_value.list_recipients.return_value = SimpleNamespace(
            signers=[
                SimpleNamespace(
                    recipient_id="3",
                    recipient_id_guid="",
                    role_name="GSA",
                    name="Ada Lovelace",
                    email="ada@example.com",
                    routing_order=1,
                    status="completed",
                    sent_date_time=None,
                    delivered_date_time=None,
                    signed_date_time="2026-10-03T18:00:00Z",
                )
            ]
        )

        def save_signed(envelope_id, user_id, employer_id, template_name, **kwargs):
            SignedDocumentFile.objects.create(
                user_id=user_id,
                employer_id=employer_id,
                envelope_id=envelope_id,
                file_name="hey-there.pdf",
                file_path="DOCUMENTS/acme/hey-there.pdf",
                template_name=template_name,
            )

        upload.side_effect = save_signed
        self.client.force_login(self.employee)
        home = self.client.get(
            reverse("employee_home"),
            {"event": "signing_complete", "envelope": envelope.pk},
        )
        envelope.refresh_from_db()
        self.assertEqual(envelope.status, "completed")
        self.assertContains(home, "No unsigned documents.")
        self.assertContains(home, "Hey there")
        upload.assert_called_once()
        notify_hr.delay.assert_called_once()

        untouched = SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="Still unsigned",
            envelope_id="env-still",
            status="sent",
        )
        cancelled = self.client.get(
            reverse("employee_home"),
            {"event": "cancel", "envelope": untouched.pk},
        )
        untouched.refresh_from_db()
        self.assertEqual(untouched.status, "sent")
        self.assertContains(cancelled, "Still unsigned")

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
        self.assertContains(dashboard, "Back to documents")
        self.assertContains(dashboard, "employee-page")
        self.assertNotContains(dashboard, "col-lg-6")
        documents = self.client.get(reverse("employee_home"))
        self.assertNotContains(documents, "Back to documents")
        immigration = self.client.get(reverse("employee_immigration_upload"))
        self.assertContains(immigration, "Back to documents")

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

    @patch("arl.user.employee_views.queue_immigration_upload_email")
    @patch("arl.bucket.helpers.upload_to_linode_object_storage")
    def test_upload_updates_the_existing_immigration_tracker(
        self, store_file, queue_email
    ):
        self.client.force_login(self.employee)
        response = self._upload()

        self.assertRedirects(response, reverse("employee_immigration_upload"))
        store_file.assert_called_once()
        queue_email.assert_called_once()
        payload = queue_email.call_args.args[0]
        self.assertEqual(payload["employer_id"], self.employer.id)
        self.assertEqual(payload["document_title"], "Work permit extension")
        self.assertEqual(payload["status_label"], "Work Permit Extension")
        self.assertTrue(payload["document_file_id"])

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
        self.assertContains(follow, "Work permit expiration")
        self.assertContains(follow, "March 1, 2027")
        self.assertContains(follow, "Overall status")
        self.assertContains(follow, "Authorized")
        self.assertNotContains(follow, "Permit expiry:")

    def test_immigration_page_shows_existing_tracker_dates(self):
        self.employee.sin_expiration_date = date(2026, 12, 1)
        self.employee.work_permit_expiration_date = date(2027, 6, 1)
        self.employee.save(
            update_fields=["sin_expiration_date", "work_permit_expiration_date"]
        )
        self.client.force_login(self.employee)

        response = self.client.get(reverse("employee_immigration_upload"))
        self.assertContains(response, "SIN expiration")
        self.assertContains(response, "December 1, 2026")
        self.assertContains(response, "Work permit expiration")
        self.assertContains(response, "June 1, 2027")
        self.assertNotContains(response, "Permit expiry:")

    def test_immigration_page_shows_not_on_file_for_missing_tracker_dates(self):
        self.client.force_login(self.employee)

        response = self.client.get(reverse("employee_immigration_upload"))
        self.assertContains(response, "SIN expiration")
        self.assertContains(response, "Work permit expiration")
        self.assertNotContains(response, "Permit expiry:")
        self.assertContains(response, "Not on file")

    def test_immigration_page_marks_soon_expiration_dates(self):
        set_user_sin(self.employee, "900000001", validate_luhn=True, save=True)
        self.employee.sin_expiration_date = date.today() + timedelta(days=30)
        self.employee.work_permit_expiration_date = date.today() + timedelta(days=45)
        self.employee.save(
            update_fields=["sin_expiration_date", "work_permit_expiration_date"]
        )
        self.client.force_login(self.employee)

        tracker = build_immigration_tracker(self.employee)
        self.assertEqual(tracker["sin_badge"]["label"], "Expiring")
        self.assertEqual(tracker["permit_badge"]["label"], "Expiring")
        self.assertEqual(tracker["overall_badge"]["label"], "Expiring soon")

        response = self.client.get(reverse("employee_immigration_upload"))
        self.assertContains(response, "Overall status")
        self.assertContains(response, "Expiring soon")
        self.assertContains(response, "Expiring", count=2)
        self.assertContains(response, "btn-immigration-save")
        self.assertNotContains(response, "btn-primary")

    def test_resolve_immigration_email_group_name_prefers_canonical_group(self):
        Group.objects.create(name=IMMIGRATION_UPLOAD_EMAIL_GROUP)
        self.assertEqual(
            resolve_immigration_email_group_name(),
            IMMIGRATION_UPLOAD_EMAIL_GROUP,
        )

    def test_resolve_immigration_email_group_name_matches_single_variant(self):
        Group.objects.create(name="ImmigrationEmail")
        self.assertEqual(resolve_immigration_email_group_name(), "ImmigrationEmail")

    def test_immigration_upload_recipient_emails_are_scoped_to_employer(self):
        group = Group.objects.create(name=IMMIGRATION_UPLOAD_EMAIL_GROUP)
        recipient = self._user(
            "imm",
            "Imm",
            "Notify",
            "+14161234570",
            self.employer,
        )
        recipient.groups.add(group)
        outsider = self._user(
            "other",
            "Other",
            "Employer",
            "+14161234571",
            self.other_employer,
        )
        outsider.groups.add(group)

        emails = immigration_upload_recipient_emails(self.employer.id)
        self.assertEqual(emails, [recipient.email])

    @patch("arl.documentflow.services_immigration.create_master_email", return_value=True)
    @patch("arl.documentflow.services_immigration.read_s3_object_bytes")
    def test_send_immigration_upload_notification_attaches_file(
        self, read_s3, send_email
    ):
        Group.objects.create(name=IMMIGRATION_UPLOAD_EMAIL_GROUP)
        recipient = self._user(
            "imm2",
            "Imm",
            "Mail",
            "+14161234572",
            self.employer,
        )
        recipient.groups.add(Group.objects.get(name=IMMIGRATION_UPLOAD_EMAIL_GROUP))
        document = SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="imm-doc",
            file_name="permit.pdf",
            file_path="DOCUMENTS/acme/permit.pdf",
            document_title="Work permit extension",
            is_company_document=False,
        )
        read_s3.return_value = b"%PDF-1.4 permit"

        result = send_immigration_upload_notification(
            employer_id=self.employer.id,
            employee_name="Ada Lovelace",
            company_name=self.employer.name,
            document_title="Work permit extension",
            status_label="Work Permit Extension",
            document_file_id=document.id,
            effective_date="2026-03-01",
            expiry_date="2027-03-01",
            reference_number="IRCC-42",
            notes="Submitted online",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["group_name"], IMMIGRATION_UPLOAD_EMAIL_GROUP)
        read_s3.assert_called_once_with("DOCUMENTS/acme/permit.pdf")
        send_email.assert_called_once()
        kwargs = send_email.call_args.kwargs
        self.assertEqual(kwargs["to_email"], [recipient.email])
        self.assertEqual(len(kwargs["attachments"]), 1)
        self.assertEqual(kwargs["attachments"][0]["filename"], "permit.pdf")
        self.assertEqual(kwargs["attachments"][0]["type"], "application/pdf")

    @patch("arl.documentflow.services_immigration.create_master_email", return_value=False)
    @patch("arl.documentflow.services_immigration.read_s3_object_bytes", return_value=b"pdf")
    def test_send_immigration_upload_notification_reports_sendgrid_failure(
        self, read_s3, send_email
    ):
        Group.objects.create(name=IMMIGRATION_UPLOAD_EMAIL_GROUP)
        recipient = self._user(
            "imm3",
            "Imm",
            "Fail",
            "+14161234573",
            self.employer,
        )
        recipient.groups.add(Group.objects.get(name=IMMIGRATION_UPLOAD_EMAIL_GROUP))
        document = SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="imm-doc-2",
            file_name="permit.pdf",
            file_path="DOCUMENTS/acme/permit-2.pdf",
            document_title="Work permit extension",
            is_company_document=False,
        )

        result = send_immigration_upload_notification(
            employer_id=self.employer.id,
            employee_name="Ada Lovelace",
            company_name=self.employer.name,
            document_title="Work permit extension",
            status_label="Work Permit Extension",
            document_file_id=document.id,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "SendGrid send failed.")

    def test_immigration_page_shows_valid_permit_status(self):
        set_user_sin(self.employee, "900000001", validate_luhn=True, save=True)
        self.employee.sin_expiration_date = date.today() + timedelta(days=200)
        self.employee.work_permit_expiration_date = date.today() + timedelta(days=400)
        self.employee.save(
            update_fields=["sin_expiration_date", "work_permit_expiration_date"]
        )
        self.client.force_login(self.employee)

        response = self.client.get(reverse("employee_immigration_upload"))
        self.assertContains(response, "Valid")
        self.assertContains(response, "Compliant")

    @patch("arl.user.employee_views.queue_immigration_upload_email")
    @patch("arl.bucket.helpers.upload_to_linode_object_storage")
    def test_new_work_permit_clears_the_extension_flag(self, store_file, queue_email):
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


class EmployeeFormsAccessTests(EmployeeTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store.objects.create(number=7, employer=self.employer)
        self.other_store = Store.objects.create(number=99, employer=self.other_employer)
        manager_group = Group.objects.create(name="Manager")
        self.manager = self._user(
            "mia",
            "Mia",
            "Manager",
            "+14161234571",
            self.employer,
        )
        self.manager.groups.add(manager_group)

    def test_gsa_can_open_existing_salt_log_and_incident_dashboard(self):
        self.assertFalse(self.employee.has_perm("incident.add_incident"))
        self.client.force_login(self.employee)

        salt_log = self.client.get(reverse("salt_log_list"))
        incident = self.client.get(reverse("incident_dashboard"))

        self.assertEqual(salt_log.status_code, 200)
        self.assertEqual(incident.status_code, 200)
        self.assertContains(salt_log, "employee-page")
        self.assertContains(salt_log, "gsa-bottom-tabs")
        self.assertContains(salt_log, "gsa-mobile-header")
        self.assertNotContains(salt_log, "navbar-toggler")
        self.assertContains(incident, "employee-page")
        self.assertContains(salt_log, "Back to documents")
        self.assertContains(incident, "Back to documents")
        self.assertContains(salt_log, "Start salt log")
        self.assertContains(incident, "Incident dashboard")
        self.assertContains(incident, "New Incident")
        self.assertContains(incident, "Update Existing")

    @patch("arl.incident.views.process_new_incident_reports_task.delay")
    def test_gsa_incident_dashboard_lists_only_company_incidents(self, _process_incident):
        from arl.incident.models import Incident

        Incident.objects.create(
            store=self.store,
            brief_description="Our spill",
            eventdetails="Fuel on pad",
            user_employer=self.employer,
        )
        other_store = Store.objects.create(number=88, employer=self.other_employer)
        Incident.objects.create(
            store=other_store,
            brief_description="Other company",
            eventdetails="Not ours",
            user_employer=self.other_employer,
        )
        self.client.force_login(self.employee)
        response = self.client.get(reverse("incident_dashboard"))
        self.assertContains(response, "Our spill")
        self.assertNotContains(response, "Other company")

    def test_non_gsa_still_needs_incident_permission(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("create_incident"))
        self.assertEqual(response.status_code, 403)

    def test_gsa_salt_log_form_lists_only_company_stores(self):
        self.client.force_login(self.employee)
        started = self.client.post(reverse("create_salt_log"))
        salt_log = SaltLog.objects.get(user=self.employee)
        self.assertRedirects(
            started, reverse("salt_log_edit", kwargs={"pk": salt_log.pk})
        )
        response = self.client.get(reverse("salt_log_edit", kwargs={"pk": salt_log.pk}))
        self.assertContains(response, str(self.store))
        self.assertNotContains(response, str(self.other_store))

    def test_gsa_cannot_edit_coworker_salt_log(self):
        log = SaltLog.objects.create(
            user=self.coworker,
            store=self.store,
            area_salted="Back lot",
            user_employer=self.employer,
        )
        self.client.force_login(self.employee)
        response = self.client.get(reverse("salt_log_update", args=[log.pk]))
        self.assertEqual(response.status_code, 404)


class GsaPreviewTests(EmployeeTestCase):
    def setUp(self):
        super().setUp()
        SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="preview-signed",
            file_name="offer.pdf",
            file_path="DOCUMENTS/acme/offer.pdf",
            document_title="Ada signed offer",
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.employee,
            template_name="Ada handbook",
            envelope_id="env-preview",
            status="sent",
        )
        SentDocuSignEnvelope.objects.create(
            employer=self.employer,
            user=self.coworker,
            template_name="Ben handbook",
            envelope_id="env-ben",
            status="sent",
        )

    def test_staff_can_open_gsa_picker(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("gsa_preview_select"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "View as GSA")
        self.assertContains(response, "Ada Lovelace")
        self.assertContains(response, "Ben Wright")

    def test_non_staff_cannot_open_gsa_picker(self):
        self.client.force_login(self.employee)
        response = self.client.get(reverse("gsa_preview_select"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("home"))

    def test_staff_preview_shows_selected_gsa_documents_and_nav(self):
        self.client.force_login(self.staff)
        started = self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": self.employee.id},
        )
        self.assertRedirects(started, reverse("employee_home"))
        self.assertTrue(self.staff.is_employee_account is False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.staff.id)

        home = self.client.get(reverse("employee_home"))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, "Ada signed offer")
        self.assertContains(home, "Ada handbook")
        self.assertNotContains(home, "Ben handbook")
        self.assertContains(home, "GSA preview, read-only")
        self.assertContains(home, "nav-link employee-section")
        self.assertContains(home, "Salt log")
        self.assertContains(home, "Incident report")
        self.assertNotContains(home, "employee-section-links")

    def test_staff_preview_is_read_only(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": self.employee.id},
        )
        own = SentDocuSignEnvelope.objects.get(template_name="Ada handbook")
        view = self.client.get(reverse("employee_unsigned_document", args=[own.id]))
        self.assertEqual(view.status_code, 200)
        self.assertContains(view, "Ada handbook")
        sign = self.client.post(
            reverse("employee_open_unsigned_document", args=[own.id])
        )
        self.assertRedirects(sign, reverse("employee_home"))

        immigration = self.client.post(
            reverse("employee_immigration_upload"),
            {
                "document_title": "Permit",
                "immigration_status_type": "work_permit_extension",
                "file": SimpleUploadedFile(
                    "permit.pdf", b"%PDF-1.4", content_type="application/pdf"
                ),
            },
        )
        self.assertRedirects(immigration, reverse("employee_home"))
        self.assertFalse(ImmigrationStatusEvent.objects.exists())

    def test_staff_preview_exit_returns_to_home(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": self.employee.id},
        )
        ended = self.client.get(reverse("gsa_preview_exit"))
        self.assertRedirects(ended, reverse("home"))
        blocked = self.client.get(reverse("employee_home"))
        self.assertRedirects(blocked, reverse("home"))

    def test_staff_cannot_preview_gsa_from_another_company(self):
        other_gsa = self._user(
            "zoe",
            "Zoe",
            "Other",
            "+14161234599",
            self.other_employer,
        )
        other_gsa.groups.add(Group.objects.get(name="GSA"))
        self.client.force_login(self.staff)
        denied = self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": other_gsa.id},
        )
        self.assertEqual(denied.status_code, 200)
        self.assertContains(denied, "not available for preview")
        blocked = self.client.get(reverse("employee_home"))
        self.assertRedirects(blocked, reverse("home"))

    def test_staff_preview_shows_real_signed_file_names(self):
        SignedDocumentFile.objects.create(
            user=self.employee,
            employer=self.employer,
            envelope_id="generic-webhook",
            file_name="Employee Handbook.pdf",
            file_path="DOCUMENTS/acme/handbook.pdf",
            template_name="New Hire File",
        )
        self.client.force_login(self.staff)
        self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": self.employee.id},
        )
        home = self.client.get(reverse("employee_home"))
        self.assertContains(home, "Employee Handbook.pdf")
        self.assertNotContains(home, "New Hire File")

    def test_staff_preview_can_open_salt_log_dashboard(self):
        SaltLog.objects.create(
            user=self.employee,
            user_employer=self.employer,
            area_salted="Front walk",
        )
        self.client.force_login(self.staff)
        self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": self.employee.id},
        )
        salt_log = self.client.get(reverse("salt_log_list"))
        self.assertEqual(salt_log.status_code, 200)
        self.assertContains(salt_log, "Salt Log Dashboard")
        self.assertContains(salt_log, "Front walk")
        self.assertNotContains(salt_log, "GSA preview is read-only")

        started = self.client.post(reverse("create_salt_log"))
        self.assertRedirects(started, reverse("employee_home"))

    def test_staff_preview_can_open_incident_dashboard(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("gsa_preview_select"),
            {"gsa_user_id": self.employee.id},
        )
        incident = self.client.get(reverse("incident_dashboard"))
        self.assertEqual(incident.status_code, 200)
        self.assertContains(incident, "Incident dashboard")
        self.assertNotContains(incident, "GSA preview is read-only")
