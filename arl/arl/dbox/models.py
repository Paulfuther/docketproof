from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

from arl.user.models import Employer


def _get_cipher():
    """Fernet cipher from SECRET_ENCRYPTION_KEY (same pattern as TenantApiKeys)."""
    key = settings.SECRET_ENCRYPTION_KEY
    if not key:
        raise ValueError("SECRET_ENCRYPTION_KEY is not configured")
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


def encrypt_secret(raw_value):
    """Encrypt a Dropbox secret. Empty values are stored as an empty string."""
    if not raw_value:
        return ""
    if isinstance(raw_value, bytes):
        raw_value = raw_value.decode()
    return _get_cipher().encrypt(raw_value.encode()).decode()


def decrypt_secret(token):
    """Decrypt a stored Dropbox secret. Returns None when nothing is stored."""
    if not token:
        return None
    try:
        return _get_cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt Dropbox credential.") from exc


class DropboxCredentials(models.Model):
    """
    Encrypted Dropbox OAuth credentials for one Employer.

    Refresh tokens (and optional per-tenant app key/secret) are stored with
    Fernet via SECRET_ENCRYPTION_KEY. Leave app key/secret blank to reuse the
    shared DROP_BOX_KEY / DROP_BOX_SECRET from settings.
    """

    employer = models.OneToOneField(
        Employer,
        on_delete=models.CASCADE,
        related_name="dropbox_credentials",
    )
    encrypted_refresh_token = models.TextField(blank=True)
    encrypted_app_key = models.TextField(
        blank=True,
        help_text=(
            "Optional per-tenant Dropbox app key. Leave blank to use "
            "DROP_BOX_KEY from settings."
        ),
    )
    encrypted_app_secret = models.TextField(
        blank=True,
        help_text=(
            "Optional per-tenant Dropbox app secret. Leave blank to use "
            "DROP_BOX_SECRET from settings."
        ),
    )
    account_email = models.EmailField(
        blank=True,
        help_text="Non-secret identifier for this Dropbox account.",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Dropbox credentials"
        verbose_name_plural = "Dropbox credentials"

    def set_refresh_token(self, raw_value):
        self.encrypted_refresh_token = encrypt_secret(raw_value) if raw_value else ""

    def get_refresh_token(self):
        return decrypt_secret(self.encrypted_refresh_token)

    def set_app_key(self, raw_value):
        self.encrypted_app_key = encrypt_secret(raw_value) if raw_value else ""

    def get_app_key(self):
        return decrypt_secret(self.encrypted_app_key)

    def set_app_secret(self, raw_value):
        self.encrypted_app_secret = encrypt_secret(raw_value) if raw_value else ""

    def get_app_secret(self):
        return decrypt_secret(self.encrypted_app_secret)

    def has_refresh_token(self):
        return bool(self.encrypted_refresh_token)

    def __str__(self):
        status = "active" if self.is_active else "inactive"
        return f"Dropbox credentials for {self.employer.name} ({status})"
