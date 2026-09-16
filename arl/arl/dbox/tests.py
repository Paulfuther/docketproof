from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django.test import SimpleTestCase, TestCase, override_settings
from dropbox.exceptions import ApiError

from arl.dbox.helpers import generate_new_access_token, upload_to_dropbox
from arl.dbox.models import DropboxCredentials, decrypt_secret, encrypt_secret
from arl.user.models import Employer

TEST_FERNET_KEY = Fernet.generate_key().decode()


class RemovedHelperImportTests(SimpleTestCase):
    def test_old_upload_helpers_are_not_exported(self):
        import arl.dbox.helpers as helpers

        self.assertTrue(hasattr(helpers, "upload_to_dropbox"))
        self.assertTrue(hasattr(helpers, "generate_new_access_token"))
        for name in (
            "master_upload_file_to_dropbox",
            "upload_to_dropbox_quiz",
            "upload_incident_file_to_dropbox",
            "upload_major_incident_file_to_dropbox",
            "upload_any_file_to_dropbox",
        ):
            self.assertFalse(hasattr(helpers, name), name)


@override_settings(SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY)
class DropboxSecretRoundTripTests(SimpleTestCase):
    def test_encrypt_decrypt_round_trip(self):
        token = encrypt_secret("refresh-secret")
        self.assertNotEqual(token, "refresh-secret")
        self.assertEqual(decrypt_secret(token), "refresh-secret")

    def test_empty_values_are_not_encrypted(self):
        self.assertEqual(encrypt_secret(""), "")
        self.assertIsNone(decrypt_secret(""))
        self.assertIsNone(decrypt_secret(None))


@override_settings(SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY)
class DropboxCredentialsModelTests(TestCase):
    def test_refresh_token_is_stored_encrypted(self):
        employer = Employer.objects.create(name="Acme Fuels")
        creds = DropboxCredentials(employer=employer)
        creds.set_refresh_token("tenant-refresh")
        creds.set_app_key("tenant-key")
        creds.set_app_secret("tenant-secret")
        creds.save()

        creds.refresh_from_db()
        self.assertNotEqual(creds.encrypted_refresh_token, "tenant-refresh")
        self.assertNotEqual(creds.encrypted_app_key, "tenant-key")
        self.assertNotEqual(creds.encrypted_app_secret, "tenant-secret")
        self.assertEqual(creds.get_refresh_token(), "tenant-refresh")
        self.assertEqual(creds.get_app_key(), "tenant-key")
        self.assertEqual(creds.get_app_secret(), "tenant-secret")
        self.assertTrue(creds.has_refresh_token())

    def test_str_does_not_include_secrets(self):
        employer = Employer.objects.create(name="Beta Co")
        creds = DropboxCredentials(employer=employer)
        creds.set_refresh_token("super-secret-token")
        self.assertNotIn("super-secret-token", str(creds))
        self.assertIn("Beta Co", str(creds))


@override_settings(
    SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY,
    DROP_BOX_REFRESH_TOKEN="env-refresh",
    DROP_BOX_KEY="env-key",
    DROP_BOX_SECRET="env-secret",
)
class DropboxAccessTokenTests(TestCase):
    @patch("arl.dbox.helpers.requests.post")
    def test_falls_back_to_settings_without_employer(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "env-access"}

        token = generate_new_access_token()

        self.assertEqual(token, "env-access")
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["refresh_token"], "env-refresh")
        self.assertEqual(payload["client_id"], "env-key")
        self.assertEqual(payload["client_secret"], "env-secret")

    @patch("arl.dbox.helpers.requests.post")
    def test_uses_employer_credentials_when_present(self, mock_post):
        employer = Employer.objects.create(name="Tenant Co")
        creds = DropboxCredentials(employer=employer, is_active=True)
        creds.set_refresh_token("tenant-refresh")
        creds.set_app_key("tenant-key")
        creds.set_app_secret("tenant-secret")
        creds.save()
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "tenant-access"}

        token = generate_new_access_token(employer=employer)

        self.assertEqual(token, "tenant-access")
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["refresh_token"], "tenant-refresh")
        self.assertEqual(payload["client_id"], "tenant-key")
        self.assertEqual(payload["client_secret"], "tenant-secret")

    @patch("arl.dbox.helpers.requests.post")
    def test_tenant_app_key_falls_back_to_settings(self, mock_post):
        employer = Employer.objects.create(name="Shared App Co")
        creds = DropboxCredentials(employer=employer, is_active=True)
        creds.set_refresh_token("tenant-refresh")
        creds.save()
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "tenant-access"}

        generate_new_access_token(employer=employer)

        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["refresh_token"], "tenant-refresh")
        self.assertEqual(payload["client_id"], "env-key")
        self.assertEqual(payload["client_secret"], "env-secret")

    @patch("arl.dbox.helpers.requests.post")
    def test_inactive_credentials_fall_back_to_settings(self, mock_post):
        employer = Employer.objects.create(name="Inactive Co")
        creds = DropboxCredentials(employer=employer, is_active=False)
        creds.set_refresh_token("inactive-refresh")
        creds.save()
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "env-access"}

        generate_new_access_token(employer=employer)

        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["refresh_token"], "env-refresh")


@override_settings(
    SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY,
    DROP_BOX_REFRESH_TOKEN="env-refresh",
    DROP_BOX_KEY="env-key",
    DROP_BOX_SECRET="env-secret",
)
class UploadToDropboxTests(SimpleTestCase):
    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_uploads_to_caller_path_with_add_mode(self, _mock_token, mock_dropbox):
        client = mock_dropbox.return_value

        ok, message = upload_to_dropbox(b"pdf-bytes", "/SALTLOGS/file.pdf")

        self.assertTrue(ok)
        self.assertIn("/SALTLOGS/file.pdf", message)
        client.files_upload.assert_called_once()
        args, kwargs = client.files_upload.call_args
        self.assertEqual(args[0], b"pdf-bytes")
        self.assertEqual(args[1], "/SALTLOGS/file.pdf")
        self.assertTrue(kwargs["mode"].is_add())

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_overwrite_write_mode(self, mock_token, mock_dropbox):
        employer = MagicMock()
        client = mock_dropbox.return_value

        ok, _message = upload_to_dropbox(
            b"zip-bytes",
            "/NEWHRFILES/hire.zip",
            employer=employer,
            write_mode="overwrite",
        )

        self.assertTrue(ok)
        mock_token.assert_called_once_with(employer=employer)
        args, kwargs = client.files_upload.call_args
        self.assertEqual(args[1], "/NEWHRFILES/hire.zip")
        self.assertTrue(kwargs["mode"].is_overwrite())

    @patch("arl.dbox.helpers.generate_new_access_token", return_value=None)
    def test_missing_credentials_returns_failure(self, _mock_token):
        ok, message = upload_to_dropbox(b"x", "/CHECKLISTS/a.pdf")
        self.assertFalse(ok)
        self.assertIn("credentials", message.lower())

    def test_missing_path_returns_failure(self):
        ok, message = upload_to_dropbox(b"x", "")
        self.assertFalse(ok)
        self.assertIn("path", message.lower())

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_api_error_is_returned(self, _mock_token, mock_dropbox):
        client = mock_dropbox.return_value
        client.files_upload.side_effect = ApiError(
            request_id="req",
            error="boom",
            user_message_text="boom",
            user_message_locale="en",
        )

        ok, message = upload_to_dropbox(b"x", "/SITEINCIDENTS/a.pdf")
        self.assertFalse(ok)
        self.assertIn("Dropbox API Error", message)
