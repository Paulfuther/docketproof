import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth.hashers import check_password
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from phonenumbers import PhoneNumberFormat, format_number, parse

from arl.user.forms import YouStepForm
from arl.user.models import CustomUser, Employer, NewHireInvite, Store
from arl.user.registration import SESSION_KEY, load_invite

PERMANENT_SIN = "130692544"
TEMPORARY_SIN = "900000001"
PASSWORD = "Dock3t-Proof-Hire!"
FERNET_TEST_KEY = "iNKsr5HSwIPaWnIPgLRQLSluZRH0zEzZnJ_f6WdFZBQ="
SIN_SETTINGS = dict(
    SECRET_KEY="ci-test-secret-key-not-for-production",
    FERNET_PRIMARY_KEY=FERNET_TEST_KEY,
    FERNET_OLD_KEYS=[],
    SIN_HASH_SALT="registration-test-salt",
)


def e164(raw):
    return format_number(parse(raw, "CA"), PhoneNumberFormat.E164)


@override_settings(**SIN_SETTINGS)
class NewHireRegistrationTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme Fuels")
        self.store = Store.objects.create(
            number=12,
            employer=self.employer,
            address="10 King St",
            city="Toronto",
            province="ON",
        )
        self.invite = self._invite()
        token_patch = patch("arl.user.registration.request_verification_token")
        check_patch = patch(
            "arl.user.registration.check_verification_token", return_value=True
        )
        chain_patch = patch("arl.user.views.chain")
        delay_patch = patch("arl.user.registration.save_user_to_db.delay")
        self.request_token = token_patch.start()
        self.check_token = check_patch.start()
        self.chain = chain_patch.start()
        self.delay = delay_patch.start()
        self.addCleanup(token_patch.stop)
        self.addCleanup(check_patch.stop)
        self.addCleanup(chain_patch.stop)
        self.addCleanup(delay_patch.stop)

    def _invite(self, **kwargs):
        defaults = {
            "employer": self.employer,
            "email": "alex.rivera@example.com",
            "name": "Alex Rivera",
            "role": "GSA",
        }
        defaults.update(kwargs)
        return NewHireInvite.objects.create(**defaults)

    def _url(self, invite=None):
        invite = invite or self.invite
        return reverse("register", args=[invite.token])

    def _post(self, data, invite=None):
        return self.client.post(self._url(invite), data, follow=True)

    def _draft(self, invite=None):
        invite = invite or self.invite
        return self.client.session[SESSION_KEY][invite.token]

    def _verify_phone(self, raw="4165550100", invite=None):
        response = self._post(
            {"step": "phone", "action": "send_code", "phone_number": raw},
            invite=invite,
        )
        self.assertContains(response, "We texted a code")
        response = self._post(
            {"step": "phone", "action": "verify_code", "verification_code": "123456"},
            invite=invite,
        )
        self.assertContains(response, "Phone verified.")
        return response

    def _you_data(self, **overrides):
        data = {
            "step": "you",
            "action": "save_you",
            "username": "alex.rivera",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "first_name": "Alex",
            "last_name": "Rivera",
            "dob": "1995-04-12",
            "address": "10 King St",
            "address_two": "Unit 2",
            "city": "Toronto",
            "state_province": "ON",
            "country": "CA",
            "postal": "m5v2t6",
            "sin_input": PERMANENT_SIN,
            "sin": PERMANENT_SIN,
            "sin_expiration_date": "",
            "work_permit_expiration_date": "",
        }
        data.update(overrides)
        return data

    def _through_confirm(self, you_overrides=None, raw_phone="4165550100"):
        self._verify_phone(raw_phone)
        you_data = self._you_data(**(you_overrides or {}))
        response = self._post(you_data)
        self.assertContains(response, "Where you'll work", html=True)
        response = self._post(
            {
                "step": "work",
                "action": "save_work",
                "store": str(self.store.pk),
                "sin": you_data["sin_input"],
            }
        )
        self.assertContains(response, "Submit registration")
        return response

    def _hr_email_payload(self):
        found = None
        for call in self.chain.call_args_list:
            for arg in call.args:
                args = getattr(arg, "args", None)
                if args and isinstance(args[0], dict) and "sin_number" in args[0]:
                    found = args[0]
        return found

    def test_fresh_link_shows_phone_step(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Join Today")
        self.assertContains(response, "Verify your phone")
        self.assertContains(response, "1. Phone")
        self.assertContains(response, "2. You")
        self.assertContains(response, "3. Work")
        self.assertContains(response, "4. Confirm")
        self.assertContains(response, "font-size: 16px")
        self.assertNotContains(response, "Submit registration")

    def test_unknown_token_is_not_found(self):
        request = RequestFactory().get("/register/missing-token/")
        with self.assertRaises(Http404):
            load_invite(request, "missing-token")

    def test_used_invite_is_explained(self):
        self.invite.used = True
        self.invite.save(update_fields=["used"])
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already been used")
        self.assertNotContains(response, "Join Today")

    def test_cannot_skip_ahead(self):
        response = self.client.get(self._url() + "?step=confirm", follow=True)
        self.assertContains(response, "Finish the earlier steps first.")
        self.assertContains(response, "Text me a code")
        self.assertEqual(CustomUser.objects.count(), 0)

    def test_invalid_phone_shows_field_error(self):
        response = self._post(
            {"step": "phone", "action": "send_code", "phone_number": "123"}
        )
        self.assertContains(response, "That phone number doesn&#x27;t look valid.")
        self.assertEqual(self.request_token.call_count, 0)
        self.assertEqual(CustomUser.objects.count(), 0)

    def test_duplicate_phone_is_rejected(self):
        CustomUser.objects.create_user(
            username="existing.phone",
            email="existing.phone@example.com",
            password=PASSWORD,
            phone_number=e164("4165550100"),
            employer=self.employer,
        )
        response = self._post(
            {"step": "phone", "action": "send_code", "phone_number": "4165550100"}
        )
        self.assertContains(response, "already registered")
        self.assertEqual(self.request_token.call_count, 0)

    def test_wrong_code_does_not_advance(self):
        self._post(
            {"step": "phone", "action": "send_code", "phone_number": "4165550100"}
        )
        self.check_token.return_value = False
        response = self._post(
            {"step": "phone", "action": "verify_code", "verification_code": "000000"}
        )
        self.assertContains(response, "doesn&#x27;t match")
        self.assertContains(response, "Text message code")
        self.assertFalse(self._draft()["phone_verified"])

    def test_resend_is_throttled(self):
        self._post(
            {"step": "phone", "action": "send_code", "phone_number": "4165550100"}
        )
        response = self._post({"step": "phone", "action": "send_code"})
        self.assertContains(response, "Wait a few seconds")
        self.assertEqual(self.request_token.call_count, 1)

    def test_empty_identity_step_lists_field_errors(self):
        self._verify_phone()
        response = self._post({"step": "you", "action": "save_you"})
        self.assertContains(response, "Enter your first name.")
        self.assertContains(response, "Enter the 9 digits of your SIN.")
        self.assertEqual(CustomUser.objects.count(), 0)

    def test_invalid_sin_is_rejected(self):
        self._verify_phone()
        response = self._post(self._you_data(sin_input="123456789"))
        self.assertContains(response, "aren&#x27;t a valid SIN")
        self.assertEqual(CustomUser.objects.count(), 0)

    def _error_count(self, response, message):
        return response.content.decode().count(message)

    def test_temporary_sin_requires_expiry_dates(self):
        self._verify_phone()
        response = self._post(self._you_data(sin_input=TEMPORARY_SIN))
        # The page script always includes each message once. Field errors add more.
        self.assertGreater(self._error_count(response, YouStepForm.TEMPORARY_SIN_EXPIRY_ERROR), 1)
        self.assertGreater(self._error_count(response, YouStepForm.TEMPORARY_SIN_PERMIT_ERROR), 1)
        html = response.content.decode()
        self.assertIn('data-temporary-sin-date="sin"', html)
        self.assertRegex(html, r'data-temporary-sin-date="sin"[^>]*\brequired\b')
        self.assertEqual(CustomUser.objects.count(), 0)

    def test_temporary_sin_with_spaces_or_dashes_requires_both_dates(self):
        self._verify_phone()
        response = self._post(self._you_data(sin_input="900-000-001"))
        self.assertGreater(self._error_count(response, YouStepForm.TEMPORARY_SIN_EXPIRY_ERROR), 1)
        self.assertGreater(self._error_count(response, YouStepForm.TEMPORARY_SIN_PERMIT_ERROR), 1)
        self.assertNotContains(response, "Where you'll work")

        response = self._post(
            self._you_data(
                sin_input="9 0000 0001",
                sin_expiration_date="2027-01-15",
            )
        )
        self.assertEqual(self._error_count(response, YouStepForm.TEMPORARY_SIN_EXPIRY_ERROR), 1)
        self.assertGreater(self._error_count(response, YouStepForm.TEMPORARY_SIN_PERMIT_ERROR), 1)
        self.assertNotContains(response, "Where you'll work")
        self.assertEqual(CustomUser.objects.count(), 0)

    def test_permanent_sin_does_not_require_expiry_dates(self):
        self._verify_phone()
        response = self._post(self._you_data(sin_input="130 692-544"))
        self.assertContains(response, "Where you'll work", html=True)
        self.assertNotContains(response, "start with 9")
        self.assertFalse(self._draft()["you"]["sin_expiration_date"])
        self.assertFalse(self._draft()["you"]["work_permit_expiration_date"])
        self.assertFalse(self._draft()["you"]["temporary_sin"])

    def test_you_step_marks_dates_required_only_when_sin_starts_with_9(self):
        temporary = YouStepForm(data={"sin_input": " 9 00-000-001 "})
        self.assertTrue(temporary.fields["sin_expiration_date"].widget.attrs.get("required"))
        self.assertEqual(
            temporary.fields["work_permit_expiration_date"].widget.attrs.get("aria-required"),
            "true",
        )
        permanent = YouStepForm(data={"sin_input": "130-692-544"})
        self.assertNotIn("required", permanent.fields["sin_expiration_date"].widget.attrs)
        self.assertNotIn(
            "required", permanent.fields["work_permit_expiration_date"].widget.attrs
        )
        blank = YouStepForm()
        self.assertNotIn("required", blank.fields["sin_expiration_date"].widget.attrs)

        self._verify_phone()
        page = self.client.get(f"{self._url()}?step=you")
        self.assertContains(page, "data-temporary-sin-input")
        self.assertContains(page, 'data-temporary-sin-date="sin"')
        self.assertContains(page, 'data-temporary-sin-date="permit"')
        self.assertContains(page, YouStepForm.TEMPORARY_SIN_EXPIRY_ERROR)
        self.assertContains(page, YouStepForm.TEMPORARY_SIN_PERMIT_ERROR)

    def test_you_step_stores_ciphertext_not_plaintext(self):
        self._verify_phone()
        self._post(self._you_data(sin_input="130 692 544"))
        draft = self._draft()
        blob = json.dumps(draft)
        self.assertNotIn(PERMANENT_SIN, blob)
        self.assertNotIn("130 692 544", blob)
        self.assertNotIn(PASSWORD, blob)
        self.assertNotEqual(draft["you"]["sin_encrypted"], PERMANENT_SIN)
        self.assertEqual(draft["you"]["sin_last4"], "2544")
        self.assertTrue(draft["you"]["password_hash"])
        self.assertNotIn("sin", draft["you"])

    def test_missing_store_is_a_visible_error(self):
        self.store.is_active = False
        self.store.save(update_fields=["is_active"])
        self._verify_phone()
        self._post(self._you_data())
        response = self._post({"step": "work", "action": "save_work"})
        self.assertContains(response, "no active stores")
        self.assertFalse(self._draft()["work_complete"])
        self.assertEqual(CustomUser.objects.count(), 0)

    def test_other_invite_does_not_reuse_verification(self):
        self._verify_phone()
        other = self._invite(email="other.hire@example.com", name="Other Hire")
        response = self.client.get(self._url(other))
        self.assertContains(response, "Text me a code")
        self.assertNotContains(response, "Phone verified")

    def test_submit_writes_encrypted_sin_and_leaves_legacy_column_empty(self):
        confirm = self._through_confirm()
        self.assertNotContains(confirm, PERMANENT_SIN)
        self.assertContains(confirm, "*** *** 2544")
        response = self._post(
            {"step": "confirm", "action": "submit", "sin": PERMANENT_SIN}
        )
        self.assertContains(response, "You're registered", html=True)
        self.assertNotContains(response, PERMANENT_SIN)

        user = CustomUser.objects.get(username="alex.rivera")
        self.assertIsNone(user.sin)
        self.assertTrue(user.sin_encrypted)
        self.assertNotEqual(user.sin_encrypted, PERMANENT_SIN)
        self.assertNotIn(PERMANENT_SIN, user.sin_encrypted)
        self.assertEqual(user.sin_last4, "2544")
        self.assertEqual(len(user.sin_hash), 64)
        self.assertEqual(user.sin_plain, PERMANENT_SIN)
        self.assertTrue(check_password(PASSWORD, user.password))
        self.assertEqual(user.email, self.invite.email)
        self.assertEqual(user.phone_number.as_e164, e164("4165550100"))
        self.assertEqual(user.store, self.store)
        self.assertEqual(user.postal, "M5V 2T6")
        self.assertEqual(user.dob, date(1995, 4, 12))
        self.assertTrue(user.groups.filter(name="GSA").exists())

        self.invite.refresh_from_db()
        self.assertTrue(self.invite.used)

        payload = self.delay.call_args.kwargs
        self.assertNotIn("sin", payload)
        self.assertNotIn("sin_input", payload)
        self.assertNotIn(PERMANENT_SIN, json.dumps(payload))

        email_data = self._hr_email_payload()
        self.assertIsNotNone(email_data)
        self.assertEqual(email_data["sin_number"], PERMANENT_SIN)

        again = self.client.get(self._url())
        self.assertContains(again, "already been used")
        self.assertNotContains(again, "Join Today")

    def test_temporary_sin_saves_expiry_dates(self):
        self._verify_phone(raw="6135550101")
        self._post(
            self._you_data(
                username="temp.worker",
                sin_input=TEMPORARY_SIN,
                sin_expiration_date="2027-01-15",
                work_permit_expiration_date="2027-06-01",
            )
        )
        self._post({"step": "work", "action": "save_work", "store": str(self.store.pk)})
        response = self._post({"step": "confirm", "action": "submit"})
        self.assertContains(response, "You're registered", html=True)
        user = CustomUser.objects.get(username="temp.worker")
        self.assertIsNone(user.sin)
        self.assertEqual(user.sin_plain, TEMPORARY_SIN)
        self.assertEqual(user.sin_expiration_date, date(2027, 1, 15))
        self.assertEqual(user.work_permit_expiration_date, date(2027, 6, 1))

    def test_confirm_rejects_temporary_sin_if_dates_are_removed(self):
        self._verify_phone(raw="4165550104")
        self._post(
            self._you_data(
                username="strip.dates",
                sin_input="900 000 001",
                sin_expiration_date="2027-01-15",
                work_permit_expiration_date="2027-06-01",
            )
        )
        self._post({"step": "work", "action": "save_work", "store": str(self.store.pk)})
        session = self.client.session
        you = session[SESSION_KEY][self.invite.token]["you"]
        you["sin_expiration_date"] = ""
        you["work_permit_expiration_date"] = ""
        session.modified = True
        session.save()
        response = self._post({"step": "confirm", "action": "submit"})
        self.assertContains(response, "add the SIN and work permit expiry dates")
        self.assertFalse(CustomUser.objects.filter(username="strip.dates").exists())

    def test_employer_role_does_not_require_a_store(self):
        invite = self._invite(
            email="boss@example.com", name="Pat Boss", role="EMPLOYER"
        )
        self._verify_phone(raw="6045550102", invite=invite)
        self._post(
            self._you_data(username="pat.boss"),
            invite=invite,
        )
        response = self._post(
            {"step": "work", "action": "save_work"},
            invite=invite,
        )
        self.assertContains(response, "Submit registration")
        response = self._post(
            {"step": "confirm", "action": "submit"},
            invite=invite,
        )
        self.assertContains(response, "You're registered", html=True)
        user = CustomUser.objects.get(username="pat.boss")
        self.assertIsNone(user.store)
        self.assertIsNone(user.sin)
        self.assertEqual(user.sin_plain, PERMANENT_SIN)
        self.assertTrue(user.groups.filter(name="EMPLOYER").exists())

    def test_complete_page_requires_a_finished_registration(self):
        response = self.client.get(reverse("register_complete"))
        self.assertRedirects(response, reverse("home"))
