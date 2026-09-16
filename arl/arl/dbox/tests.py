"""
Dropbox helper and credential tests.

All cases are Django SimpleTestCase tests. They mock the Dropbox SDK and
ORM lookups, so they do not need a live Dropbox account or Postgres.

Run:

    python arl/manage.py test arl.dbox

manage.py still needs the project's usual env vars (SECRET_KEY,
SECRET_ENCRYPTION_KEY, FERNET_PRIMARY_KEY, SIN_HASH_SALT). Because these
tests never hit the database, Django will not create a test Postgres DB
when this app is run by itself.
"""
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from django import forms
from django.contrib.admin.sites import AdminSite
from django.test import SimpleTestCase, override_settings
from dropbox.exceptions import ApiError

from arl.dbox.admin import DropboxCredentialsAdmin, DropboxCredentialsForm
from arl.dbox.helpers import (
    DROPBOX_TOKEN_URL,
    _credentials_for_employer,
    generate_new_access_token,
    upload_to_dropbox,
)
from arl.dbox.models import DropboxCredentials, decrypt_secret, encrypt_secret
from arl.user.models import Employer

TEST_FERNET_KEY = Fernet.generate_key().decode()
ARL_DIR = Path(__file__).resolve().parent.parent

REMOVED_HELPERS = (
    "master_upload_file_to_dropbox",
    "upload_to_dropbox_quiz",
    "upload_incident_file_to_dropbox",
    "upload_major_incident_file_to_dropbox",
    "upload_any_file_to_dropbox",
)

CALLER_FILES = (
    ARL_DIR / "quiz" / "tasks.py",
    ARL_DIR / "reclose" / "tasks.py",
    ARL_DIR / "incident" / "tasks.py",
    ARL_DIR / "dsign" / "helpers.py",
    ARL_DIR / "dbox" / "views.py",
)

ENV_DROPBOX_SETTINGS = dict(
    SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY,
    DROP_BOX_REFRESH_TOKEN="env-refresh",
    DROP_BOX_KEY="env-key",
    DROP_BOX_SECRET="env-secret",
)


def _employer(name="Acme Fuels"):
    employer = MagicMock()
    employer.name = name
    employer.pk = 17
    return employer


def _tenant_creds(
    refresh="tenant-refresh", app_key="tenant-key", app_secret="tenant-secret"
):
    creds = MagicMock()
    creds.get_refresh_token.return_value = refresh
    creds.get_app_key.return_value = app_key
    creds.get_app_secret.return_value = app_secret
    return creds


@override_settings(SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY)
class DropboxSecretRoundTripTests(SimpleTestCase):
    def test_encrypt_decrypt_round_trip(self):
        token = encrypt_secret("refresh-secret")
        self.assertNotEqual(token, "refresh-secret")
        self.assertEqual(decrypt_secret(token), "refresh-secret")

    def test_encrypt_accepts_bytes(self):
        token = encrypt_secret(b"key-secret")
        self.assertEqual(decrypt_secret(token), "key-secret")

    def test_empty_values_are_not_encrypted(self):
        self.assertEqual(encrypt_secret(""), "")
        self.assertEqual(encrypt_secret(None), "")
        self.assertIsNone(decrypt_secret(""))
        self.assertIsNone(decrypt_secret(None))

    def test_invalid_ciphertext_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            decrypt_secret("not-a-fernet-token")
        self.assertIn("decrypt", str(ctx.exception).lower())

    def test_wrong_fernet_key_cannot_decrypt(self):
        token = encrypt_secret("refresh-secret")
        other_key = Fernet.generate_key().decode()
        with override_settings(SECRET_ENCRYPTION_KEY=other_key):
            with self.assertRaises(ValueError):
                decrypt_secret(token)

    def test_missing_encryption_key_raises(self):
        with override_settings(SECRET_ENCRYPTION_KEY=None):
            with self.assertRaises(ValueError):
                encrypt_secret("refresh-secret")


@override_settings(SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY)
class DropboxCredentialsModelTests(SimpleTestCase):
    """
    Field values on DropboxCredentials are the columns Django persists.
    These tests do not require Postgres; they assert secrets never sit in
    plaintext on the model fields that map to the database.
    """

    def _unsaved_creds(self, **secrets):
        creds = DropboxCredentials(employer=Employer(name="Acme Fuels"))
        if "refresh" in secrets:
            creds.set_refresh_token(secrets["refresh"])
        if "app_key" in secrets:
            creds.set_app_key(secrets["app_key"])
        if "app_secret" in secrets:
            creds.set_app_secret(secrets["app_secret"])
        return creds

    def _column(self, creds, field_name):
        return creds._meta.get_field(field_name).value_from_object(creds)

    def test_refresh_token_and_app_secrets_are_encrypted_on_model_fields(self):
        creds = self._unsaved_creds(
            refresh="tenant-refresh",
            app_key="tenant-key",
            app_secret="tenant-secret",
        )
        self.assertNotEqual(self._column(creds, "encrypted_refresh_token"), "tenant-refresh")
        self.assertNotEqual(self._column(creds, "encrypted_app_key"), "tenant-key")
        self.assertNotEqual(self._column(creds, "encrypted_app_secret"), "tenant-secret")
        self.assertEqual(creds.get_refresh_token(), "tenant-refresh")
        self.assertEqual(creds.get_app_key(), "tenant-key")
        self.assertEqual(creds.get_app_secret(), "tenant-secret")
        self.assertTrue(creds.has_refresh_token())

    def test_plaintext_never_appears_in_persisted_column_values(self):
        secrets = ("super-secret-refresh", "super-secret-key", "super-secret-secret")
        creds = self._unsaved_creds(
            refresh=secrets[0], app_key=secrets[1], app_secret=secrets[2]
        )
        persisted = {
            "encrypted_refresh_token": self._column(creds, "encrypted_refresh_token"),
            "encrypted_app_key": self._column(creds, "encrypted_app_key"),
            "encrypted_app_secret": self._column(creds, "encrypted_app_secret"),
            "account_email": "",
            "is_active": True,
        }
        blob = " ".join(str(value) for value in persisted.values())
        for secret in secrets:
            self.assertNotIn(secret, blob)
            self.assertNotIn(secret, creds.encrypted_refresh_token)
            self.assertNotIn(secret, creds.encrypted_app_key)
            self.assertNotIn(secret, creds.encrypted_app_secret)

    def test_clearing_refresh_token_stores_empty_not_plaintext(self):
        creds = self._unsaved_creds(refresh="tenant-refresh")
        creds.set_refresh_token("")
        self.assertEqual(creds.encrypted_refresh_token, "")
        self.assertIsNone(creds.get_refresh_token())
        self.assertFalse(creds.has_refresh_token())

    def test_blank_app_key_and_secret_stay_empty(self):
        creds = self._unsaved_creds(refresh="tenant-refresh")
        self.assertEqual(creds.encrypted_app_key, "")
        self.assertEqual(creds.encrypted_app_secret, "")
        self.assertIsNone(creds.get_app_key())
        self.assertIsNone(creds.get_app_secret())

    def test_str_does_not_include_secrets(self):
        creds = self._unsaved_creds(refresh="super-secret-token")
        text = str(creds)
        self.assertNotIn("super-secret-token", text)
        self.assertIn("Acme Fuels", text)
        self.assertIn("active", text)

        creds.is_active = False
        self.assertIn("inactive", str(creds))
        self.assertNotIn("super-secret-token", str(creds))

    def test_has_refresh_token_false_when_unset(self):
        creds = DropboxCredentials()
        self.assertFalse(creds.has_refresh_token())


class DropboxCredentialsLookupTests(SimpleTestCase):
    def test_missing_employer_does_not_query(self):
        with patch("arl.dbox.models.DropboxCredentials") as model:
            self.assertIsNone(_credentials_for_employer(None))
            self.assertIsNone(_credentials_for_employer(False))
            model.objects.filter.assert_not_called()

    def test_lookup_filters_active_rows_only(self):
        employer = _employer()
        active_row = MagicMock()
        with patch("arl.dbox.models.DropboxCredentials") as model:
            model.objects.filter.return_value.first.return_value = active_row
            found = _credentials_for_employer(employer)
        model.objects.filter.assert_called_once_with(
            employer=employer, is_active=True
        )
        self.assertIs(found, active_row)

    def test_missing_row_returns_none(self):
        with patch("arl.dbox.models.DropboxCredentials") as model:
            model.objects.filter.return_value.first.return_value = None
            self.assertIsNone(_credentials_for_employer(_employer()))


@override_settings(**ENV_DROPBOX_SETTINGS)
class DropboxAccessTokenTests(SimpleTestCase):
    def _post_ok(self, mock_post, access_token="access"):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": access_token}

    def _payload(self, mock_post):
        return mock_post.call_args.kwargs["data"]

    @patch("arl.dbox.helpers.requests.post")
    def test_legacy_env_path_when_no_employer(self, mock_post):
        self._post_ok(mock_post, "env-access")
        token = generate_new_access_token()
        self.assertEqual(token, "env-access")
        payload = self._payload(mock_post)
        self.assertEqual(payload["refresh_token"], "env-refresh")
        self.assertEqual(payload["client_id"], "env-key")
        self.assertEqual(payload["client_secret"], "env-secret")
        self.assertEqual(payload["grant_type"], "refresh_token")
        self.assertEqual(mock_post.call_args.args[0], DROPBOX_TOKEN_URL)

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer", return_value=None)
    def test_legacy_env_path_when_employer_has_no_row(self, _mock_lookup, mock_post):
        self._post_ok(mock_post, "env-access")
        token = generate_new_access_token(employer=_employer())
        self.assertEqual(token, "env-access")
        self.assertEqual(self._payload(mock_post)["refresh_token"], "env-refresh")

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_inactive_or_missing_active_row_uses_env(self, mock_lookup, mock_post):
        # _credentials_for_employer already filters is_active=True, so inactive
        # rows look like a missing row to token generation.
        mock_lookup.return_value = None
        self._post_ok(mock_post, "env-access")
        token = generate_new_access_token(employer=_employer())
        self.assertEqual(token, "env-access")
        self.assertEqual(self._payload(mock_post)["refresh_token"], "env-refresh")

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_active_employer_credentials_are_used(self, mock_lookup, mock_post):
        mock_lookup.return_value = _tenant_creds()
        self._post_ok(mock_post, "tenant-access")
        token = generate_new_access_token(employer=_employer())
        self.assertEqual(token, "tenant-access")
        payload = self._payload(mock_post)
        self.assertEqual(payload["refresh_token"], "tenant-refresh")
        self.assertEqual(payload["client_id"], "tenant-key")
        self.assertEqual(payload["client_secret"], "tenant-secret")

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_active_row_does_not_fall_back_to_env_refresh_token(
        self, mock_lookup, mock_post
    ):
        mock_lookup.return_value = _tenant_creds()
        self._post_ok(mock_post, "tenant-access")
        generate_new_access_token(employer=_employer())
        payload = self._payload(mock_post)
        self.assertEqual(payload["refresh_token"], "tenant-refresh")
        self.assertNotEqual(payload["refresh_token"], "env-refresh")

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_active_row_with_blank_refresh_does_not_use_env(
        self, mock_lookup, mock_post
    ):
        mock_lookup.return_value = _tenant_creds(refresh=None)
        with patch("arl.dbox.helpers.logger"):
            token = generate_new_access_token(employer=_employer())
        self.assertIsNone(token)
        mock_post.assert_not_called()

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_decrypt_failure_does_not_use_env_refresh(self, mock_lookup, mock_post):
        creds = _tenant_creds()
        creds.get_refresh_token.side_effect = ValueError("Unable to decrypt")
        mock_lookup.return_value = creds
        with patch("arl.dbox.helpers.logger"):
            token = generate_new_access_token(employer=_employer())
        self.assertIsNone(token)
        mock_post.assert_not_called()

    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_blank_tenant_app_key_secret_use_shared_settings(
        self, mock_lookup, mock_post
    ):
        mock_lookup.return_value = _tenant_creds(app_key=None, app_secret="")
        self._post_ok(mock_post, "tenant-access")
        generate_new_access_token(employer=_employer())
        payload = self._payload(mock_post)
        self.assertEqual(payload["refresh_token"], "tenant-refresh")
        self.assertEqual(payload["client_id"], "env-key")
        self.assertEqual(payload["client_secret"], "env-secret")

    @patch("arl.dbox.helpers.requests.post")
    def test_token_endpoint_error_returns_none(self, mock_post):
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = "invalid_grant"
        with patch("arl.dbox.helpers.logger"):
            self.assertIsNone(generate_new_access_token())

    @patch("arl.dbox.helpers.requests.post")
    def test_missing_env_refresh_returns_none(self, mock_post):
        with override_settings(DROP_BOX_REFRESH_TOKEN=None):
            with patch("arl.dbox.helpers.logger"):
                self.assertIsNone(generate_new_access_token())
        mock_post.assert_not_called()


@override_settings(**ENV_DROPBOX_SETTINGS)
class UploadToDropboxTests(SimpleTestCase):
    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_success_uploads_caller_path_with_add_mode(self, mock_token, mock_dropbox):
        client = mock_dropbox.return_value
        path = "/SALTLOGS/acme/2026/09-September/store-12/salt_log.pdf"
        ok, message = upload_to_dropbox(b"pdf-bytes", path)

        self.assertTrue(ok)
        self.assertEqual(message, f"Uploaded file to Dropbox at {path}.")
        mock_token.assert_called_once_with(employer=None)
        mock_dropbox.assert_called_once_with("access-token")
        args, kwargs = client.files_upload.call_args
        self.assertEqual(args[0], b"pdf-bytes")
        self.assertEqual(args[1], path)
        self.assertTrue(kwargs["mode"].is_add())

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_overwrite_write_mode_is_passed_through(self, mock_token, mock_dropbox):
        employer = _employer()
        ok, _message = upload_to_dropbox(
            b"zip-bytes",
            "/NEWHRFILES/hire.zip",
            employer=employer,
            write_mode="overwrite",
        )
        self.assertTrue(ok)
        mock_token.assert_called_once_with(employer=employer)
        args, kwargs = mock_dropbox.return_value.files_upload.call_args
        self.assertEqual(args[1], "/NEWHRFILES/hire.zip")
        self.assertTrue(kwargs["mode"].is_overwrite())
        self.assertFalse(kwargs["mode"].is_add())

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_known_caller_destinations_are_used_as_given(self, _mock_token, mock_dropbox):
        client = mock_dropbox.return_value
        destinations = (
            "/SALTLOGS/file.pdf",
            "/CHECKLISTS/file.pdf",
            "/RECANDCLOSE/file.pdf",
            "/SITEINCIDENTS/file.pdf",
            "/NEWHRFILES/hire.zip",
            "/NEWHIREQUIZ/quiz.zip",
        )
        for path in destinations:
            client.files_upload.reset_mock()
            ok, message = upload_to_dropbox(b"x", path)
            self.assertTrue(ok, path)
            self.assertIn(path, message)
            self.assertEqual(client.files_upload.call_args.args[1], path)

    @patch("arl.dbox.helpers.generate_new_access_token", return_value=None)
    def test_missing_credentials_returns_failure(self, _mock_token):
        ok, message = upload_to_dropbox(b"x", "/CHECKLISTS/a.pdf")
        self.assertFalse(ok)
        self.assertIsInstance(message, str)
        self.assertIn("credentials", message.lower())

    def test_missing_path_returns_failure_without_sdk(self):
        with patch("arl.dbox.helpers.dropbox.Dropbox") as mock_dropbox:
            ok, message = upload_to_dropbox(b"x", "")
            self.assertFalse(ok)
            self.assertIn("path", message.lower())
            mock_dropbox.assert_not_called()

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_api_error_returns_false_and_message(self, _mock_token, mock_dropbox):
        mock_dropbox.return_value.files_upload.side_effect = ApiError(
            request_id="req",
            error="boom",
            user_message_text="boom",
            user_message_locale="en",
        )
        with patch("arl.dbox.helpers.logger"):
            ok, message = upload_to_dropbox(b"x", "/SITEINCIDENTS/a.pdf")
        self.assertFalse(ok)
        self.assertIn("Dropbox API Error", message)

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.generate_new_access_token", return_value="access-token")
    def test_unexpected_error_returns_false_and_message(self, _mock_token, mock_dropbox):
        mock_dropbox.return_value.files_upload.side_effect = RuntimeError("network down")
        with patch("arl.dbox.helpers.logger"):
            ok, message = upload_to_dropbox(b"x", "/CHECKLISTS/a.pdf")
        self.assertFalse(ok)
        self.assertTrue(message.startswith("Error:"))
        self.assertIn("network down", message)

    @patch("arl.dbox.helpers.dropbox.Dropbox")
    @patch("arl.dbox.helpers.requests.post")
    @patch("arl.dbox.helpers._credentials_for_employer")
    def test_upload_resolves_tenant_token_then_uploads(
        self, mock_lookup, mock_post, mock_dropbox
    ):
        mock_lookup.return_value = _tenant_creds(app_key=None, app_secret=None)
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"access_token": "tenant-access"}
        employer = _employer()

        ok, message = upload_to_dropbox(
            b"pdf-bytes",
            "/SITEINCIDENTS/report.pdf",
            employer=employer,
            write_mode="add",
        )

        self.assertTrue(ok)
        self.assertIn("/SITEINCIDENTS/report.pdf", message)
        mock_dropbox.assert_called_once_with("tenant-access")
        payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(payload["refresh_token"], "tenant-refresh")
        self.assertNotEqual(payload["refresh_token"], "env-refresh")
        self.assertEqual(payload["client_id"], "env-key")
        self.assertEqual(payload["client_secret"], "env-secret")


class RemovedHelperImportTests(SimpleTestCase):
    def test_old_upload_helpers_are_not_exported(self):
        import arl.dbox.helpers as helpers

        self.assertTrue(hasattr(helpers, "upload_to_dropbox"))
        self.assertTrue(hasattr(helpers, "generate_new_access_token"))
        for name in REMOVED_HELPERS:
            self.assertFalse(hasattr(helpers, name), name)

    def test_repo_python_files_do_not_import_removed_helpers(self):
        hits = []
        for path in ARL_DIR.rglob("*.py"):
            if path.name == "tests.py" and path.parent.name == "dbox":
                continue
            source = path.read_text(encoding="utf-8")
            for name in REMOVED_HELPERS:
                if name in source:
                    hits.append(f"{path.relative_to(ARL_DIR)}:{name}")
        self.assertEqual(hits, [], msg="Removed Dropbox helpers still referenced")

    def test_callers_import_unified_helper(self):
        for path in CALLER_FILES:
            source = path.read_text(encoding="utf-8")
            self.assertIn(
                "upload_to_dropbox",
                source,
                msg=f"{path} should call upload_to_dropbox",
            )
            self.assertNotIn("master_upload_file_to_dropbox", source)
            imported = self._imported_names(source)
            self.assertIn(
                "upload_to_dropbox",
                imported,
                msg=f"{path} should import upload_to_dropbox",
            )

    def test_callers_keep_folder_destinations_and_write_modes(self):
        quiz = (ARL_DIR / "quiz" / "tasks.py").read_text(encoding="utf-8")
        reclose = (ARL_DIR / "reclose" / "tasks.py").read_text(encoding="utf-8")
        incident = (ARL_DIR / "incident" / "tasks.py").read_text(encoding="utf-8")
        dsign = (ARL_DIR / "dsign" / "helpers.py").read_text(encoding="utf-8")
        views = (ARL_DIR / "dbox" / "views.py").read_text(encoding="utf-8")

        self.assertIn("/SALTLOGS/", quiz)
        self.assertIn("/CHECKLISTS/", quiz)
        self.assertIn("/RECANDCLOSE/", reclose)
        self.assertIn("/SITEINCIDENTS/", incident)
        self.assertIn("/NEWHRFILES/", dsign)
        self.assertIn("/NEWHIREQUIZ/", dsign)

        self.assertIn('write_mode="add"', quiz)
        self.assertIn('write_mode="add"', reclose)
        self.assertIn('write_mode="add"', incident)
        self.assertIn('write_mode="add"', views)
        self.assertIn('write_mode="overwrite"', dsign)
        self.assertNotIn("files_upload", views)

        tasks_dir_sources = quiz + reclose + incident
        self.assertNotIn("import dropbox", tasks_dir_sources)
        self.assertNotIn("from dropbox", tasks_dir_sources)

    def _imported_names(self, source):
        tree = ast.parse(source)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(alias.name)
        return names


@override_settings(SECRET_ENCRYPTION_KEY=TEST_FERNET_KEY)
class DropboxAdminSafetyTests(SimpleTestCase):
    def setUp(self):
        self.model_admin = DropboxCredentialsAdmin(DropboxCredentials, AdminSite())

    def test_encrypted_fields_are_excluded_from_admin(self):
        encrypted = (
            "encrypted_refresh_token",
            "encrypted_app_key",
            "encrypted_app_secret",
        )
        for field in encrypted:
            self.assertNotIn(field, self.model_admin.list_display)
            self.assertNotIn(field, self.model_admin.search_fields)
            self.assertIn(field, self.model_admin.exclude)

    def test_secret_form_fields_are_write_only_passwords(self):
        for name in ("refresh_token", "app_key", "app_secret"):
            field = DropboxCredentialsForm.base_fields[name]
            self.assertIsInstance(field.widget, forms.PasswordInput)
            self.assertFalse(field.widget.render_value)
            self.assertFalse(field.required)

    def test_list_display_has_safe_columns_only(self):
        self.assertEqual(
            self.model_admin.list_display,
            (
                "employer",
                "account_email",
                "is_active",
                "has_refresh_token",
                "created_at",
                "updated_at",
            ),
        )

    def test_admin_has_refresh_token_flag_does_not_decrypt(self):
        creds = DropboxCredentials()
        creds.pk = 1
        creds.encrypted_refresh_token = encrypt_secret("hidden-refresh")
        self.assertTrue(self.model_admin.has_refresh_token(creds))
        creds.encrypted_refresh_token = ""
        self.assertFalse(self.model_admin.has_refresh_token(creds))

    def test_form_save_encrypts_when_secrets_provided(self):
        instance = DropboxCredentials()
        form = DropboxCredentialsForm(instance=instance)
        form.cleaned_data = {
            "refresh_token": "plain-refresh",
            "app_key": "plain-key",
            "app_secret": "plain-secret",
        }
        with patch.object(forms.ModelForm, "save", return_value=instance):
            saved = form.save(commit=False)
        self.assertNotEqual(saved.encrypted_refresh_token, "plain-refresh")
        self.assertNotEqual(saved.encrypted_app_key, "plain-key")
        self.assertNotEqual(saved.encrypted_app_secret, "plain-secret")
        self.assertEqual(saved.get_refresh_token(), "plain-refresh")
        self.assertEqual(saved.get_app_key(), "plain-key")
        self.assertEqual(saved.get_app_secret(), "plain-secret")

    def test_form_save_keeps_existing_secrets_when_fields_blank(self):
        instance = DropboxCredentials()
        instance.set_refresh_token("existing-refresh")
        original = instance.encrypted_refresh_token
        form = DropboxCredentialsForm(instance=instance)
        form.cleaned_data = {
            "refresh_token": "",
            "app_key": "",
            "app_secret": "",
        }
        with patch.object(forms.ModelForm, "save", return_value=instance):
            saved = form.save(commit=False)
        self.assertEqual(saved.encrypted_refresh_token, original)
        self.assertEqual(saved.get_refresh_token(), "existing-refresh")
