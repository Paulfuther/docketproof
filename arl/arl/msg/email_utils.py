"""Helpers for in-app email templates and logging the resolved subject."""

import json
import re
from io import BytesIO

from django.utils.html import escape
from PIL import Image, ImageOps


# Default wrapper template used by compose-your-own messages.
# Override with settings.SENDGRID_GENERIC_TEMPLATE_ID when Django is configured.
GENERIC_SENDGRID_TEMPLATE_ID = "d-4ac0497efd864e29b4471754a9c836eb"
# Email clients clip wide images; keep inline body art within a common
# single-column width. Headers are smaller and must not stretch to 100%.
EMAIL_IMAGE_MAX_WIDTH = 600
EMAIL_HEADER_MAX_WIDTH = 600
EMAIL_HEADER_MAX_HEIGHT = 180
EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT = 280
EMAIL_HEADER_DISPLAY_WIDTH_CHOICES = [
    (180, "Small (180px)"),
    (240, "Compact (240px)"),
    (280, "Medium (280px)"),
    (360, "Large (360px)"),
    (440, "Wide (440px)"),
    (520, "Full (520px)"),
]
EMAIL_JPEG_QUALITY = 80


def get_generic_sendgrid_template_id():
    try:
        from django.conf import settings

        return getattr(
            settings, "SENDGRID_GENERIC_TEMPLATE_ID", GENERIC_SENDGRID_TEMPLATE_ID
        )
    except Exception:
        return GENERIC_SENDGRID_TEMPLATE_ID


EMAIL_SOURCE_IN_APP = "in_app"
EMAIL_SOURCE_SENDGRID = "sendgrid"
EMAIL_SOURCE_COMPOSE = "compose"
COMPOSE_TEMPLATE_NAME = "Compose"
EMAIL_SOURCE_CHOICES = [
    (EMAIL_SOURCE_IN_APP, "In-app"),
    (EMAIL_SOURCE_SENDGRID, "SendGrid"),
    (EMAIL_SOURCE_COMPOSE, "Compose"),
]


def email_source_label(source, sendgrid_id=None):
    if source:
        return dict(EMAIL_SOURCE_CHOICES).get(source, source)
    if sendgrid_id:
        return "SendGrid"
    return ""


def infer_email_source(source=None, html_body=None, sendgrid_id=None):
    if source:
        return source
    if html_body and str(html_body).strip():
        return EMAIL_SOURCE_IN_APP
    sid = (sendgrid_id or "").strip()
    if sid and sid != get_generic_sendgrid_template_id():
        return EMAIL_SOURCE_SENDGRID
    return EMAIL_SOURCE_COMPOSE


def friendly_template_name(source, template_name=None, sendgrid_id=None):
    name = (template_name or "").strip()
    if name:
        return name
    if source == EMAIL_SOURCE_COMPOSE:
        return COMPOSE_TEMPLATE_NAME
    if source == EMAIL_SOURCE_IN_APP:
        return "In-app template"
    sid = (sendgrid_id or "").strip()
    return sid


def logged_sendgrid_id(source, sendgrid_id=None):
    """SendGrid dynamic template id to persist; empty for in-app HTML."""
    sid = (sendgrid_id or "").strip()
    generic = get_generic_sendgrid_template_id()
    if source == EMAIL_SOURCE_IN_APP:
        if sid and sid != generic:
            return sid
        return ""
    if source == EMAIL_SOURCE_COMPOSE:
        return sid or generic
    return sid


def parse_app_template_id(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def email_identity(
    source=None,
    html_body=None,
    sendgrid_id=None,
    template_name=None,
    app_template_id=None,
):
    """Human-facing template identity for logs, unique_args, and the webhook."""
    resolved_source = infer_email_source(
        source=source, html_body=html_body, sendgrid_id=sendgrid_id
    )
    name = friendly_template_name(
        resolved_source, template_name=template_name, sendgrid_id=sendgrid_id
    )
    sid = logged_sendgrid_id(resolved_source, sendgrid_id=sendgrid_id)
    return {
        "source": resolved_source,
        "template_name": name,
        "sendgrid_id": sid,
        "app_template_id": parse_app_template_id(app_template_id),
    }


def build_email_custom_args(
    subject,
    template_name,
    source,
    sendgrid_id=None,
    app_template_id=None,
):
    args = {
        "subject": subject or "",
        "template_name": template_name or "",
        "source": source or "",
    }
    if sendgrid_id:
        args["sendgrid_id"] = str(sendgrid_id)
    if app_template_id not in (None, ""):
        args["app_template_id"] = str(app_template_id)
    return args


def sendgrid_unique_args(event_data):
    """Merge nested unique/custom args and flattened copies of our keys."""
    merged = {}
    if not isinstance(event_data, dict):
        return merged
    for key in ("unique_args", "custom_args"):
        extra = event_data.get(key)
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (TypeError, ValueError):
                extra = None
        if isinstance(extra, dict):
            for k, value in extra.items():
                if value is None or value == "":
                    continue
                merged[str(k)] = value
    for k in (
        "subject",
        "template_name",
        "source",
        "sendgrid_id",
        "app_template_id",
    ):
        value = event_data.get(k)
        if value not in (None, "") and k not in merged:
            merged[k] = value
    return merged


def extract_sendgrid_event_meta(event_data):
    """Subject + template identity from a SendGrid Event Webhook payload."""
    empty = {
        "subject": None,
        "template_name": "",
        "sendgrid_id": "",
        "source": "",
        "app_template_id": None,
    }
    if not isinstance(event_data, dict):
        return empty
    extra = sendgrid_unique_args(event_data)
    native_name = (event_data.get("sg_template_name") or "").strip()
    native_id = (event_data.get("sg_template_id") or "").strip()
    generic = get_generic_sendgrid_template_id()
    subject = event_data.get("subject") or extra.get("subject") or None
    if subject:
        subject = str(subject)
    source = str(extra.get("source") or "").strip()
    extra_name = str(extra.get("template_name") or "").strip()
    extra_sid = str(extra.get("sendgrid_id") or "").strip()
    # In-app HTML and compose use unique_args; do not let the generic
    # wrapper template overwrite the human-facing name. Legacy dynamic
    # templates keep SendGrid's native sg_template_id / name.
    if source in (EMAIL_SOURCE_IN_APP, EMAIL_SOURCE_COMPOSE):
        template_name = extra_name or native_name
        if source == EMAIL_SOURCE_IN_APP:
            sendgrid_id = extra_sid
            if sendgrid_id == generic:
                sendgrid_id = ""
            if not sendgrid_id and native_id and native_id != generic:
                sendgrid_id = native_id
        else:
            sendgrid_id = extra_sid or native_id
    else:
        template_name = native_name or extra_name
        sendgrid_id = native_id or extra_sid
        if not source and native_id:
            source = EMAIL_SOURCE_SENDGRID
    return {
        "subject": subject,
        "template_name": template_name,
        "sendgrid_id": sendgrid_id,
        "source": source,
        "app_template_id": parse_app_template_id(extra.get("app_template_id")),
    }


def email_event_template_q(sendgrid_id=None, app_template_id=None):
    """Match EmailEvents for a template by SendGrid id and/or app pk."""
    from django.db.models import Q

    q = Q()
    sid = (sendgrid_id or "").strip()
    if sid:
        q |= Q(sg_template_id=sid)
    pk = parse_app_template_id(app_template_id)
    if pk:
        q |= Q(app_template_id=pk)
    return q


_TRIPLE_BRACE = re.compile(r"\{\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}\}")
_DOUBLE_BRACE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def render_merge_fields(text, context):
    """Render SendGrid-style {{var}} / {{{var}}} placeholders.

    Triple braces insert the value unescaped (HTML bodies). Double braces
    HTML-escape the value. Unknown keys are left in place.
    """
    if not text:
        return ""
    context = context or {}

    def replace_triple(match):
        key = match.group(1)
        if key not in context or context[key] is None:
            return match.group(0)
        return str(context[key])

    def replace_double(match):
        key = match.group(1)
        if key not in context or context[key] is None:
            return match.group(0)
        return escape(str(context[key]))

    rendered = _TRIPLE_BRACE.sub(replace_triple, text)
    return _DOUBLE_BRACE.sub(replace_double, rendered)


def resolve_email_subject(subject=None, template=None, employer=None):
    """Pick a subject from user input, then the template, then a fallback."""
    if subject and str(subject).strip():
        return str(subject).strip()
    if template is not None:
        template_subject = (getattr(template, "subject", None) or "").strip()
        if template_subject:
            return template_subject
        template_name = (getattr(template, "name", None) or "").strip()
        if template_name:
            return template_name
    employer_name = getattr(employer, "name", None) if employer is not None else None
    if employer_name:
        return f"New Message from {employer_name}"
    return "New Message from Our Company"


def sample_preview_context(user=None, employer=None):
    employer = employer or getattr(user, "employer", None)
    name = ""
    if user is not None:
        name = (user.get_full_name() or "").strip() or getattr(user, "username", "")
    return {
        "name": name or "Jane Doe",
        "company_name": getattr(employer, "name", None) or "Example Company",
        "senior_contact_name": getattr(employer, "senior_contact_name", None)
        or "Alex Manager",
        "subject": "Sample Subject",
        "body": "",
    }


def extract_sendgrid_event_subject(event_data):
    """Read subject from a SendGrid Event Webhook payload."""
    return extract_sendgrid_event_meta(event_data).get("subject")


def sendgrid_id_for_template(template):
    """Return the SendGrid template id used as transport.

    In-app templates (HTML stored in the app DB) send as raw HTML via
    SendGrid; the generic wrapper id is only used as a fallback log key.
    """
    if template is None:
        return GENERIC_SENDGRID_TEMPLATE_ID
    if getattr(template, "html_body", None) and str(template.html_body).strip():
        return (template.sendgrid_id or "").strip() or GENERIC_SENDGRID_TEMPLATE_ID
    return (getattr(template, "sendgrid_id", None) or "").strip() or GENERIC_SENDGRID_TEMPLATE_ID


def _lanczos():
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:
        return Image.LANCZOS


def _image_has_alpha(image):
    if image.mode in ("RGBA", "LA"):
        return True
    if image.mode == "P" and "transparency" in image.info:
        return True
    return False


def prepare_email_image(
    file_obj, filename="", max_width=None, max_height=None, crop=False
):
    """Resize/compress an image for inline email use.

    Caps width at ``max_width`` (default EMAIL_IMAGE_MAX_WIDTH) and optionally
    height. ``crop=True`` center-crops to that box (banner). Opaque images
    become JPEG; images with transparency stay PNG. Returns
    ``(BytesIO, extension_without_dot, content_type)``.
    """
    max_width = max_width or EMAIL_IMAGE_MAX_WIDTH
    if hasattr(file_obj, "seek"):
        try:
            file_obj.seek(0)
        except Exception:
            pass

    image = Image.open(file_obj)
    try:
        image = ImageOps.exif_transpose(image)
    except Exception:
        pass

    if getattr(image, "n_frames", 1) > 1:
        image.seek(0)

    image = _resize_email_image(
        image, max_width=max_width, max_height=max_height, crop=crop
    )

    buffer = BytesIO()
    if _image_has_alpha(image):
        if image.mode not in ("RGBA", "LA"):
            image = image.convert("RGBA")
        image.save(buffer, format="PNG", optimize=True)
        buffer.seek(0)
        return buffer, "png", "image/png"

    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(
        buffer,
        format="JPEG",
        quality=EMAIL_JPEG_QUALITY,
        optimize=True,
        progressive=True,
    )
    buffer.seek(0)
    return buffer, "jpg", "image/jpeg"


def _resize_email_image(image, max_width, max_height=None, crop=False):
    width, height = image.size
    if width < 1 or height < 1:
        return image
    if crop and max_height:
        target_w = min(int(max_width), width)
        target_h = min(int(max_height), height)
        banner_h = max(1, int(round(target_w * max_height / float(max_width))))
        if banner_h <= height:
            target_h = banner_h
        else:
            target_h = height
            target_w = max(1, int(round(target_h * max_width / float(max_height))))
            target_w = min(target_w, width)
        return ImageOps.fit(
            image,
            (max(1, target_w), max(1, target_h)),
            method=_lanczos(),
            centering=(0.5, 0.35),
        )
    box_h = int(max_height) if max_height else 10**6
    if width > max_width or height > box_h:
        image = image.copy()
        image.thumbnail((int(max_width), box_h), _lanczos())
    return image


def prepare_header_image(file_obj, filename=""):
    """Fit any upload into a 600×180 email-header banner (center-crop)."""
    return prepare_email_image(
        file_obj,
        filename=filename,
        max_width=EMAIL_HEADER_MAX_WIDTH,
        max_height=EMAIL_HEADER_MAX_HEIGHT,
        crop=True,
    )


def resolve_header_display_width(width=None):
    allowed = {choice[0] for choice in EMAIL_HEADER_DISPLAY_WIDTH_CHOICES}
    try:
        value = int(width)
    except (TypeError, ValueError):
        return EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT
    if value in allowed:
        return value
    return max(120, min(EMAIL_HEADER_MAX_WIDTH, value))


def wrap_in_app_email_html(
    html_body, header_image_url=None, header_display_width=None
):
    """Prepend an optional header image above in-app template HTML.

    Uses a fixed pixel width (not width:100%) plus HTML width attributes so
    Outlook and other clients do not stretch the header into a giant banner.
    """
    body = html_body or ""
    url = (header_image_url or "").strip()
    if not url:
        return body
    safe_url = escape(url)
    width = resolve_header_display_width(header_display_width)
    header = (
        f'<table role="presentation" align="center" border="0" cellpadding="0" '
        f'cellspacing="0" width="{width}" '
        f'style="margin:0 auto 16px auto;width:{width}px;max-width:{width}px;">'
        '<tr><td align="center" style="padding:0;">'
        f'<img src="{safe_url}" alt="" width="{width}" '
        f'style="display:block;width:{width}px;max-width:{width}px;height:auto;'
        'border:0;outline:none;text-decoration:none;">'
        "</td></tr></table>\n"
    )
    return header + body
