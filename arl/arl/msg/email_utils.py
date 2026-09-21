"""Helpers for in-app email templates and logging the resolved subject."""

import json
import re

from django.utils.html import escape


# Default wrapper template used by compose-your-own messages.
# Override with settings.SENDGRID_GENERIC_TEMPLATE_ID when Django is configured.
GENERIC_SENDGRID_TEMPLATE_ID = "d-4ac0497efd864e29b4471754a9c836eb"


def get_generic_sendgrid_template_id():
    try:
        from django.conf import settings

        return getattr(
            settings, "SENDGRID_GENERIC_TEMPLATE_ID", GENERIC_SENDGRID_TEMPLATE_ID
        )
    except Exception:
        return GENERIC_SENDGRID_TEMPLATE_ID


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
    """Read subject from a SendGrid Event Webhook payload.

    SendGrid does not reliably include ``subject`` for dynamic-template
    sends. We pass the resolved subject as a custom/unique arg at send
    time so it shows up here either flattened or nested.
    """
    if not isinstance(event_data, dict):
        return None

    subject = event_data.get("subject")
    if subject:
        return str(subject)

    for key in ("unique_args", "custom_args"):
        extra = event_data.get(key)
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except (TypeError, ValueError):
                extra = None
        if isinstance(extra, dict) and extra.get("subject"):
            return str(extra.get("subject"))

    return None


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
