from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from arl.user.models import Employer, EmployerSettings, NewHireInvite


@override_settings(SECRET_KEY="ci-test-secret-key-not-for-production")
class NewHireInviteExpiryTests(TestCase):
    def setUp(self):
        self.employer = Employer.objects.create(name="Acme Fuels")

    def _invite(self, **kwargs):
        defaults = {
            "employer": self.employer,
            "email": "newhire@example.com",
            "name": "New Hire",
            "role": "GSA",
        }
        defaults.update(kwargs)
        return NewHireInvite.objects.create(**defaults)

    def test_new_invite_defaults_to_14_days(self):
        invite = self._invite()
        self.assertIsNotNone(invite.expires_at)
        self.assertFalse(invite.is_expired())
        self.assertAlmostEqual(
            (invite.expires_at - invite.created_at).total_seconds(),
            14 * 24 * 3600,
            delta=5,
        )

    def test_legacy_null_expires_at_never_times_out(self):
        invite = self._invite()
        NewHireInvite.objects.filter(pk=invite.pk).update(expires_at=None)
        invite.refresh_from_db()
        self.assertIsNone(invite.expires_at)
        self.assertFalse(invite.is_expired())

    def test_expired_register_link_shows_message_not_404(self):
        invite = self._invite()
        invite.expires_at = timezone.now() - timedelta(hours=1)
        invite.save(update_fields=["expires_at"])
        resp = self.client.get(reverse("register", args=[invite.token]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "expired")
        self.assertNotContains(resp, "Join Today")

    def test_fresh_register_link_shows_form(self):
        invite = self._invite(email="fresh@example.com")
        resp = self.client.get(reverse("register", args=[invite.token]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Join Today")

    def test_employer_zero_days_never_expires(self):
        EmployerSettings.objects.create(
            employer=self.employer,
            new_hire_invite_expiry_days=0,
        )
        invite = self._invite(email="forever@example.com")
        self.assertIsNone(invite.expires_at)
        self.assertFalse(invite.is_expired())

    def test_employer_override_seven_days(self):
        EmployerSettings.objects.create(
            employer=self.employer,
            new_hire_invite_expiry_days=7,
        )
        invite = self._invite(email="week@example.com")
        self.assertAlmostEqual(
            (invite.expires_at - invite.created_at).total_seconds(),
            7 * 24 * 3600,
            delta=5,
        )

    def test_refresh_expiry_extends_expired_invite(self):
        invite = self._invite(email="resend@example.com")
        invite.expires_at = timezone.now() - timedelta(days=1)
        invite.save(update_fields=["expires_at"])
        self.assertTrue(invite.is_expired())
        invite.refresh_expiry()
        invite.refresh_from_db()
        self.assertFalse(invite.is_expired())
        self.assertAlmostEqual(
            (invite.expires_at - timezone.now()).total_seconds(),
            14 * 24 * 3600,
            delta=10,
        )

    def test_password_reset_timeout_is_14_days(self):
        from django.conf import settings

        self.assertEqual(settings.PASSWORD_RESET_TIMEOUT, 60 * 60 * 24 * 14)

    @override_settings(NEW_HIRE_INVITE_EXPIRY_DAYS=10)
    def test_site_setting_override(self):
        invite = self._invite(email="ten@example.com")
        self.assertAlmostEqual(
            (invite.expires_at - invite.created_at).total_seconds(),
            10 * 24 * 3600,
            delta=5,
        )
