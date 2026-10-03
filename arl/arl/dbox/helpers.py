import logging

import dropbox
import requests
from django.conf import settings
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode

logger = logging.getLogger(__name__)

DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"


def _credentials_for_employer(employer):
    """Return the active DropboxCredentials row for an employer, if any."""
    if not employer:
        return None
    from arl.dbox.models import DropboxCredentials

    return DropboxCredentials.objects.filter(
        employer=employer, is_active=True
    ).first()


def generate_new_access_token(employer=None):
    """
    Exchange a refresh token for a short-lived Dropbox access token.

    When an employer is provided and an active DropboxCredentials row exists,
    that tenant's encrypted refresh token is used (app key/secret fall back to
    settings if the tenant did not store its own). Otherwise the legacy
    DROP_BOX_* environment settings are used.
    """
    creds = _credentials_for_employer(employer)
    refresh_token = None
    app_key = None
    app_secret = None

    if creds is not None:
        try:
            refresh_token = creds.get_refresh_token()
            app_key = creds.get_app_key()
            app_secret = creds.get_app_secret()
        except ValueError:
            logger.exception(
                "Failed to decrypt Dropbox credentials for employer %s",
                getattr(employer, "pk", employer),
            )
            return None
        if not refresh_token:
            logger.error(
                "Active DropboxCredentials for employer %s has no refresh token.",
                getattr(employer, "pk", employer),
            )
            return None

    refresh_token = refresh_token or settings.DROP_BOX_REFRESH_TOKEN
    app_key = app_key or settings.DROP_BOX_KEY
    app_secret = app_secret or settings.DROP_BOX_SECRET

    if not refresh_token:
        logger.error("Dropbox refresh token is not configured.")
        return None

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": app_key,
        "client_secret": app_secret,
    }

    response = requests.post(DROPBOX_TOKEN_URL, data=data)
    if response.status_code == 200:
        return response.json().get("access_token")

    logger.error("Error generating Dropbox access token: %s", response.text)
    return None


def upload_to_dropbox(
    file_content, dropbox_path, *, employer=None, write_mode="add"
):
    """
    Upload file bytes to a full Dropbox path.

    Callers/tasks are responsible for building dropbox_path (folder + filename)
    and for choosing write_mode ("add" never overwrites; "overwrite" replaces).

    When employer is provided, token generation uses that tenant's encrypted
    Dropbox credentials if an active row exists; otherwise it falls back to
    DROP_BOX_* settings (legacy single-tenant).

    :return: Tuple (success: bool, message: str).
    """
    try:
        if not dropbox_path:
            return False, "Dropbox path is required."

        new_access_token = generate_new_access_token(employer=employer)
        if not new_access_token:
            return False, (
                "Dropbox credentials not found for this employer or in settings."
            )

        dbx = dropbox.Dropbox(new_access_token)
        dbx.files_upload(
            file_content, dropbox_path, mode=WriteMode(write_mode)
        )
        return True, f"Uploaded file to Dropbox at {dropbox_path}."
    except ApiError as e:
        logger.error("Dropbox API Error: %s", e)
        return False, f"Dropbox API Error: {str(e)}"
    except Exception as e:
        logger.error("Dropbox upload error: %s", e)
        return False, f"Error: {str(e)}"
