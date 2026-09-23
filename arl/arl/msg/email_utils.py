"""Helpers for in-app email templates and logging the resolved subject."""

import json
import re
from html import unescape
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
    (520, "Extra wide (520px)"),
    (600, "Full"),
]
EMAIL_HEADER_SPACE_TIGHT = 8
EMAIL_HEADER_SPACE_NORMAL = 24
EMAIL_HEADER_SPACE_ROOMY = 48
EMAIL_HEADER_SPACE_DEFAULT = EMAIL_HEADER_SPACE_NORMAL
EMAIL_HEADER_SPACE_CHOICES = [
    (EMAIL_HEADER_SPACE_TIGHT, "Tight"),
    (EMAIL_HEADER_SPACE_NORMAL, "Normal"),
    (EMAIL_HEADER_SPACE_ROOMY, "Roomy"),
]
EMAIL_HEADER_SOURCE_MAX_WIDTH = 1200
EMAIL_JPEG_QUALITY = 80
EMAIL_BODY_IMAGE_FLOAT_WIDTH = 240
BODY_IMAGE_PLACEMENTS = ("full", "left", "right")
# One modest inset for header, body, and SendGrid ASM footer.
# Do not stack this on both body and the gutter td — that pinched
# the column on iPhone. 16px is enough air without a narrow well.
EMAIL_SIDE_GUTTER_PX = 16
EMAIL_DOC_PAD = f"12px {EMAIL_SIDE_GUTTER_PX}px"


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


# Keys actually filled by master_email_send_task / sample_preview_context.
# Do not add store or CustomUser columns that the send path does not substitute.
IN_APP_MERGE_FIELDS = [
    {"group": "Person", "label": "Employee name", "tag": "name"},
    {"group": "Company", "label": "Company name", "tag": "company_name"},
    {"group": "Company", "label": "Company contact", "tag": "senior_contact_name"},
]


def in_app_merge_field_tags():
    return [field["tag"] for field in IN_APP_MERGE_FIELDS]


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


def prepare_header_image(file_obj, filename="", crop=True):
    """Prepare a header image for email.

    Default ``crop=True`` auto-fits a 600×180 banner (first upload).
    Pass ``crop=False`` for a cropper apply or “use full image”: keep the
    natural aspect and only cap width so logos are not letterboxed.
    """
    return prepare_email_image(
        file_obj,
        filename=filename,
        max_width=EMAIL_HEADER_MAX_WIDTH,
        max_height=EMAIL_HEADER_MAX_HEIGHT if crop else None,
        crop=bool(crop),
    )


def prepare_header_source_image(file_obj, filename=""):
    """Keep a wider original so Paul can reframe after the auto-fit crop."""
    return prepare_email_image(
        file_obj,
        filename=filename,
        max_width=EMAIL_HEADER_SOURCE_MAX_WIDTH,
        crop=False,
    )


def absolute_header_url(url):
    """Make a stored header URL safe to load after save/re-edit."""
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return url


def header_reframe_urls(source_url=None, header_url=None):
    """URLs to try when reopening Reframe on a saved template.

    Prefer the original source so the cropper is not stuck on a banner crop.
    Fall back to the current header if source was never stored.
    """
    urls = []
    for url in (absolute_header_url(source_url), absolute_header_url(header_url)):
        if url and url not in urls:
            urls.append(url)
    return urls


def resolve_header_display_width(width=None):
    allowed = {choice[0] for choice in EMAIL_HEADER_DISPLAY_WIDTH_CHOICES}
    try:
        value = int(width)
    except (TypeError, ValueError):
        return EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT
    if value in allowed:
        return value
    return max(120, min(EMAIL_HEADER_MAX_WIDTH, value))


def resolve_header_space_below(space=None):
    allowed = {choice[0] for choice in EMAIL_HEADER_SPACE_CHOICES}
    try:
        value = int(space)
    except (TypeError, ValueError):
        return EMAIL_HEADER_SPACE_DEFAULT
    if value in allowed:
        return value
    return EMAIL_HEADER_SPACE_DEFAULT


def header_spacer_html(space=None):
    """Outlook-safe gap under the header (td height + &nbsp;, not CSS margin)."""
    height = resolve_header_space_below(space)
    return (
        f'<table role="presentation" data-dp-header-space="1" align="center" '
        f'border="0" cellpadding="0" cellspacing="0" width="100%" '
        f'style="width:100%;max-width:{EMAIL_IMAGE_MAX_WIDTH}px;">'
        f'<tr><td height="{height}" '
        f'style="height:{height}px;line-height:{height}px;font-size:1px;">'
        "&nbsp;</td></tr></table>\n"
    )


def in_app_email_document(inner_html):
    """Full HTML document so iPhone Mail gets a device-width viewport.

    Fragments without this meta are laid out at a wide desktop canvas, which
    parks the 600px column on the left and leaves a white gap on the right.
    SendGrid appends the ASM unsubscribe footer before </body>. One
    12×16px inset on the body (not also on the gutter td) so header,
    message, and footer share the same modest air and stay nearly
    full-width on a phone.
    """
    pad = EMAIL_DOC_PAD
    return (
        "<!DOCTYPE html>"
        '<html lang="en">'
        "<head>"
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        "<title></title>"
        '<style type="text/css">'
        f"body{{margin:0!important;padding:{pad}!important;"
        "width:100%!important;}}"
        "</style>"
        "</head>"
        f'<body style="margin:0;padding:{pad};width:100%;'
        'background:#ffffff;">'
        '<table role="presentation" data-dp-email-gutter="1" width="100%" '
        'border="0" cellpadding="0" cellspacing="0" style="width:100%;">'
        "<tr>"
        '<td align="left" valign="top" style="padding:0;">'
        f"{inner_html or ''}"
        # Leave the gutter table open. SendGrid injects ASM unsubscribe
        # immediately before </body>; that footer then shares the same
        # 16px body inset as the header and message (no extra cell pad).
        "</body></html>"
    )


def wrap_in_app_email_html(
    html_body,
    header_image_url=None,
    header_display_width=None,
    header_space_below=None,
):
    """Wrap in-app HTML in a centered, mobile-fluid column.

    Header uses HTML width for Outlook and width:100% + max-width for phones
    so it sits in the available column without stretching past the chosen size.
    A 600px-only spacer is avoided: that forced a desktop canvas and left a
    right-hand gap on iPhone. Horizontal air is a single 16px body inset
    shared by header, copy, and the injected ASM footer.
    """
    body = html_body or ""
    url = (header_image_url or "").strip()
    if not url and not str(body).strip():
        return body
    parts = []
    if url:
        safe_url = escape(url)
        width = resolve_header_display_width(header_display_width)
        parts.append(
            f'<table role="presentation" align="center" border="0" cellpadding="0" '
            f'cellspacing="0" width="100%" '
            f'style="margin:0 auto;width:100%;max-width:{EMAIL_IMAGE_MAX_WIDTH}px;">'
            '<tr><td align="center" style="padding:0;">'
            f'<img src="{safe_url}" alt="" width="{width}" '
            f'style="display:block;margin:0 auto;width:100%;max-width:{width}px;'
            "height:auto;border:0;outline:none;text-decoration:none;\">"
            "</td></tr></table>\n"
        )
        parts.append(header_spacer_html(header_space_below))
    parts.append(body)
    column = (
        '<table role="presentation" data-dp-email="1" align="center" border="0" '
        'cellpadding="0" cellspacing="0" width="100%" '
        f'style="width:100%;max-width:{EMAIL_IMAGE_MAX_WIDTH}px;margin:0 auto;">'
        '<tr><td align="left" style="padding:12px 0;">'
        f"{''.join(parts)}"
        "</td></tr></table>"
    )
    return in_app_email_document(column)


def resolve_body_image_placement(placement=None):
    value = (placement or "full").strip().lower()
    if value in BODY_IMAGE_PLACEMENTS:
        return value
    return "full"


def wrap_body_image(url, placement="full"):
    """Email-safe body image. HTML body stays the source of truth.

    Left/right use align + float so text can sit beside the picture in
    clients that honor it. Outlook's Word engine often stacks them.
    """
    url = (url or "").strip()
    if not url:
        return ""
    safe = escape(url)
    place = resolve_body_image_placement(placement)
    if place in ("left", "right"):
        width = EMAIL_BODY_IMAGE_FLOAT_WIDTH
        margin = "0 16px 12px 0" if place == "left" else "0 0 12px 16px"
        return (
            f'<table role="presentation" data-dp-body-image="1" align="{place}" '
            f'border="0" cellpadding="0" cellspacing="0" width="{width}" '
            f'style="float:{place};margin:{margin};width:{width}px;max-width:100%;">'
            f'<tr><td style="padding:0;">'
            f'<img src="{safe}" alt="" width="{width}" '
            f'style="display:block;width:{width}px;max-width:100%;height:auto;'
            "border:0;outline:none;text-decoration:none;\">"
            "</td></tr></table>\n"
        )
    width = EMAIL_IMAGE_MAX_WIDTH
    return (
        f'<table role="presentation" data-dp-body-image="1" align="center" '
        f'border="0" cellpadding="0" cellspacing="0" width="{width}" '
        f'style="margin:16px auto;width:{width}px;max-width:100%;">'
        f'<tr><td align="center" style="padding:0;">'
        f'<img src="{safe}" alt="" width="{width}" '
        f'style="display:block;width:100%;max-width:{width}px;height:auto;'
        "border:0;outline:none;text-decoration:none;\">"
        "</td></tr></table>\n"
    )


def list_body_image_urls(html):
    urls = []
    for match in re.finditer(
        r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', html or "", flags=re.I
    ):
        url = match.group(1).strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def remove_body_image(html, url):
    """Strip one inserted picture (and its table/p wrapper) from HTML."""
    html = html or ""
    url = (url or "").strip()
    if not html or not url:
        return html
    escaped = re.escape(url)
    patterns = (
        rf'<table\b[^>]*\bdata-dp-body-image\b[^>]*>[\s\S]*?<img\b[^>]*\bsrc=["\']{escaped}["\'][\s\S]*?</table>\s*',
        rf'<p\b[^>]*>\s*<img\b[^>]*\bsrc=["\']{escaped}["\'][^>]*>\s*</p>\s*',
        rf'<img\b[^>]*\bsrc=["\']{escaped}["\'][^>]*>\s*',
    )
    for pattern in patterns:
        html = re.sub(pattern, "", html, flags=re.I)
    return html


_BODY_IMAGE_BLOCK_RE = re.compile(
    r"<table\b[^>]*\bdata-dp-body-image\b[^>]*>[\s\S]*?</table>\s*"
    r"|<p\b[^>]*>\s*<img\b[^>]*>\s*</p>\s*",
    flags=re.I,
)


def extract_body_image_html(html):
    return "".join(_BODY_IMAGE_BLOCK_RE.findall(html or ""))


def html_to_message_text(html):
    """Plain words from html_body, ignoring inserted pictures."""
    text = html or ""
    for url in list_body_image_urls(text):
        text = remove_body_image(text, url)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def message_text_to_html(text):
    """Turn a friendly message into simple paragraph HTML."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    blocks = []
    for part in re.split(r"\n\s*\n", text):
        lines = [escape(line) for line in part.split("\n")]
        blocks.append("<p>" + "<br>".join(lines) + "</p>")
    return "\n".join(blocks) + "\n"
