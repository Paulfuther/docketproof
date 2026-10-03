import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone as dt_timezone
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST

from arl.user.gsa_access import (
    gsa_actor,
    gsa_preview_blocks_mutation,
    is_gsa_preview_active,
    shows_gsa_chrome,
)
from arl.documentflow.constants import IMMIGRATION_STATUS_CHOICES
from arl.documentflow.models import SentDocuSignEnvelope
from arl.documentflow.services import get_step_pill_class, get_step_pill_label
from arl.documentflow.services_immigration import (
    _coerce_date,
    update_immigration_tracker,
)
from arl.dsign.models import SignedDocumentFile

logger = logging.getLogger(__name__)

UNSIGNED_STATUSES = ("created", "sent", "delivered")
DOCUMENT_PAGE_SIZE = 10
MAX_UPLOAD_MB = 25
ALLOWED_UPLOAD_CT = {
    "application/pdf",
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
}
KNOWN_EXTENSIONS = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".heic",
    ".heif",
}


def _guess_ct(uploaded):
    ct = getattr(uploaded, "content_type", None) or ""
    if ct == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(uploaded.name or "")
        return guessed or ct
    return ct


def _extension_for(uploaded, content_type):
    _, ext = os.path.splitext(uploaded.name or "")
    ext = (ext or "").lower()
    if ext in KNOWN_EXTENSIONS:
        return ext
    if content_type == "application/pdf":
        return ".pdf"
    if content_type in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if content_type == "image/png":
        return ".png"
    if content_type == "image/webp":
        return ".webp"
    if content_type == "image/gif":
        return ".gif"
    if content_type in {"image/heic", "image/heif"}:
        return ".heic"
    return ""


def _document_label(envelope):
    if envelope.flow_step_id and getattr(envelope.flow_step, "label", ""):
        return envelope.flow_step.label
    if envelope.template_name:
        return envelope.template_name
    if envelope.template_id and envelope.template:
        return envelope.template.template_name
    return "Document"


def _pill_for_envelope(envelope):
    status = (envelope.status or "").lower()
    return {
        "id": envelope.id,
        "name": _document_label(envelope),
        "label": get_step_pill_label(status),
        "pill_class": get_step_pill_class(status),
    }


def _unsigned_queryset(user):
    return (
        SentDocuSignEnvelope.objects.filter(
            user=user,
            employer=user.employer,
            status__in=UNSIGNED_STATUSES,
        )
        .select_related("template", "flow_step")
        .order_by("-sent_at", "-id")
    )


def _completed_envelopes_queryset(user):
    return (
        SentDocuSignEnvelope.objects.filter(
            user=user,
            employer=user.employer,
            status="completed",
        )
        .select_related("template", "flow_step")
        .order_by("-completed_at", "-sent_at", "-id")
    )


def _employee_signed_entries(actor):
    """Signed files plus completed envelopes that never received a stored file."""
    if not actor.employer_id:
        return []

    files = list(
        SignedDocumentFile.objects.filter(
            user=actor,
            employer=actor.employer,
            is_company_document=False,
        ).order_by("-uploaded_at", "-id")
    )
    envelopes = list(_completed_envelopes_queryset(actor))
    envelope_by_id = {
        envelope.envelope_id: envelope
        for envelope in envelopes
        if envelope.envelope_id
    }
    seen_envelope_ids = set()
    entries = []

    for document in files:
        envelope = (
            envelope_by_id.get(document.envelope_id) if document.envelope_id else None
        )
        if document.envelope_id:
            seen_envelope_ids.add(document.envelope_id)
        completed_at = None
        if envelope and envelope.completed_at:
            completed_at = envelope.completed_at
        else:
            completed_at = document.uploaded_at
        entries.append(
            {
                "name": document.document_title
                or document.template_name
                or document.file_name,
                "completed_at": completed_at,
                "download_id": document.id,
            }
        )

    for envelope in envelopes:
        if envelope.envelope_id and envelope.envelope_id in seen_envelope_ids:
            continue
        entries.append(
            {
                "name": _document_label(envelope),
                "completed_at": envelope.completed_at or envelope.sent_at,
                "download_id": None,
            }
        )

    entries.sort(
        key=lambda entry: entry["completed_at"]
        or datetime.fromtimestamp(0, tz=dt_timezone.utc),
        reverse=True,
    )
    return entries


def _envelope_for_actor(user, envelope_id):
    try:
        envelope = (
            SentDocuSignEnvelope.objects.select_related("template", "flow_step", "user")
            .get(pk=envelope_id)
        )
    except SentDocuSignEnvelope.DoesNotExist:
        raise Http404("Document not found.")
    if envelope.user_id != user.id:
        return None
    if user.employer_id and envelope.employer_id != user.employer_id:
        return None
    if (envelope.status or "").lower() not in UNSIGNED_STATUSES:
        raise Http404("Document not found.")
    return envelope


def _redirect_unless_gsa(request):
    if shows_gsa_chrome(request):
        return None
    return redirect("home")


def _employee_documents_context(request, actor):
    signed_entries = []
    unsigned_envelopes = SentDocuSignEnvelope.objects.none()
    if actor.employer_id:
        signed_entries = _employee_signed_entries(actor)
        unsigned_envelopes = _unsigned_queryset(actor)
    signed_page = Paginator(signed_entries, DOCUMENT_PAGE_SIZE).get_page(
        request.GET.get("signed")
    )
    unsigned_page = Paginator(unsigned_envelopes, DOCUMENT_PAGE_SIZE).get_page(
        request.GET.get("unsigned")
    )
    unsigned_pills = [_pill_for_envelope(envelope) for envelope in unsigned_page]
    return {
        "signed_page": signed_page,
        "unsigned_page": unsigned_page,
        "unsigned_pills": unsigned_pills,
        "document_owner": actor,
    }


def _refresh_after_embedded_signing(request):
    """DocuSign returns here after an in-app signature. Use the webhook handler."""
    event = (request.GET.get("event") or "").strip().lower()
    if event != "signing_complete":
        return
    envelope_pk = request.GET.get("envelope")
    if not envelope_pk:
        return
    try:
        envelope = SentDocuSignEnvelope.objects.select_related(
            "template", "user", "employer", "flow", "flow_step"
        ).get(pk=envelope_pk, user=request.user)
    except (SentDocuSignEnvelope.DoesNotExist, ValueError):
        return
    if request.user.employer_id and envelope.employer_id != request.user.employer_id:
        return
    try:
        from arl.dsign.tasks import refresh_sent_envelope_from_docusign

        result = refresh_sent_envelope_from_docusign(envelope)
    except Exception:
        logger.exception(
            "Could not refresh envelope %s after in-app signing", envelope_pk
        )
        return
    if isinstance(result, dict) and result.get("error"):
        logger.error(
            "In-app signing refresh for envelope %s failed: %s",
            envelope.envelope_id,
            result.get("error"),
        )


@login_required
def employee_home(request):
    denied = _redirect_unless_gsa(request)
    if denied:
        return denied
    actor = gsa_actor(request)
    if actor is None:
        return redirect("home")
    if not is_gsa_preview_active(request):
        _refresh_after_embedded_signing(request)
    return render(
        request,
        "user/employee/home.html",
        _employee_documents_context(request, actor),
    )


@login_required
def employee_unsigned_document(request, envelope_id):
    denied = _redirect_unless_gsa(request)
    if denied:
        return denied
    actor = gsa_actor(request) or request.user
    envelope = _envelope_for_actor(actor, envelope_id)
    if envelope is None:
        return HttpResponseForbidden("You cannot open this document.")
    status = (envelope.status or "").lower()
    return render(
        request,
        "user/employee/unsigned_document.html",
        {
            "envelope": envelope,
            "document_name": _document_label(envelope),
            "pill_label": get_step_pill_label(status),
            "pill_class": get_step_pill_class(status),
        },
    )


@login_required
@require_POST
def employee_open_unsigned_document(request, envelope_id):
    denied = _redirect_unless_gsa(request)
    if denied:
        return denied
    blocked = gsa_preview_blocks_mutation(request)
    if blocked:
        return blocked
    actor = gsa_actor(request) or request.user
    envelope = _envelope_for_actor(actor, envelope_id)
    if envelope is None:
        return HttpResponseForbidden("You cannot open this document.")
    try:
        from arl.dsign.helpers import get_recipient_view_url

        return_url = request.build_absolute_uri(
            f"{reverse('employee_home')}?{urlencode({'envelope': envelope.pk})}"
        )
        signing_url = get_recipient_view_url(
            user=actor,
            envelope_id=envelope.envelope_id,
            return_url=return_url,
        )
    except Exception:
        logger.exception("Unsigned document %s could not be opened", envelope.pk)
        messages.error(
            request, "This document could not be opened. Please try again."
        )
        return redirect("employee_unsigned_document", envelope_id=envelope.pk)
    if not signing_url:
        messages.error(
            request, "This document could not be opened. Please try again."
        )
        return redirect("employee_unsigned_document", envelope_id=envelope.pk)
    return redirect(signing_url)


def _expiring_soon(expiry_date, watch_days=120):
    if not expiry_date:
        return False
    return (expiry_date - timezone.localdate()).days <= watch_days


def _immigration_context(user):
    latest = (
        user.immigration_status_events.filter(is_active=True)
        .order_by("-created_at")
        .first()
    )
    sin_expiration = user.sin_expiration_date
    work_permit_expiration = user.work_permit_expiration_date
    return {
        "status_choices": IMMIGRATION_STATUS_CHOICES,
        "latest_event": latest,
        "sin_expiration": sin_expiration,
        "sin_expiring_soon": _expiring_soon(sin_expiration),
        "work_permit_expiration": work_permit_expiration,
        "work_permit_expiring_soon": _expiring_soon(work_permit_expiration),
        "extension_requested": user.work_permit_extension_requested,
        "extension_date": user.work_permit_extension_date,
    }


@login_required
@require_http_methods(["GET", "POST"])
def employee_immigration_upload(request):
    denied = _redirect_unless_gsa(request)
    if denied:
        return denied
    actor = gsa_actor(request) or request.user
    if request.method == "POST":
        blocked = gsa_preview_blocks_mutation(request)
        if blocked:
            return blocked
    user = actor
    if not user.employer_id:
        messages.error(request, "Your account is not linked to an employer yet.")
        return redirect("employee_home")

    context = _immigration_context(user)
    if request.method == "GET":
        return render(request, "user/employee/immigration_upload.html", context)

    title = (request.POST.get("document_title") or "").strip()
    status_type = (request.POST.get("immigration_status_type") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    reference = (request.POST.get("immigration_reference_number") or "").strip()
    uploaded = request.FILES.get("file")

    def _reject(message):
        messages.error(request, message)
        return render(request, "user/employee/immigration_upload.html", context)

    if not title or not status_type or uploaded is None:
        return _reject("Title, immigration status, and a file are required.")
    if status_type not in dict(IMMIGRATION_STATUS_CHOICES):
        return _reject("Choose a valid immigration status.")
    if uploaded.size == 0:
        return _reject("The file is empty.")
    size_mb = uploaded.size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        return _reject(f"File too large ({size_mb:.1f} MB). Max {MAX_UPLOAD_MB} MB.")

    content_type = _guess_ct(uploaded)
    if content_type not in ALLOWED_UPLOAD_CT:
        return _reject("Upload a PDF or image of the immigration document.")
    extension = _extension_for(uploaded, content_type)
    if not extension:
        return _reject("Upload a PDF or image of the immigration document.")

    try:
        _coerce_date(request.POST.get("immigration_effective_date"))
        _coerce_date(request.POST.get("immigration_expiry_date"))
    except ValueError as exc:
        return _reject(str(exc))

    company = slugify(user.employer.name or "company") or "company"
    today = timezone.now()
    filename = f"{slugify(title) or 'immigration'}-{uuid.uuid4().hex[:8]}{extension}"
    object_key = (
        f"DOCUMENTS/{company}/{today:%Y}/{today:%m}/{user.id}/{filename}"
    )

    try:
        from arl.bucket.helpers import upload_to_linode_object_storage

        upload_to_linode_object_storage(uploaded, object_key)
        with transaction.atomic():
            document = SignedDocumentFile.objects.create(
                user=user,
                employer=user.employer,
                envelope_id=uuid.uuid4().hex[:10],
                file_name=filename,
                file_path=object_key,
                document_title=title,
                notes=notes,
                is_company_document=False,
            )
            update_immigration_tracker(
                employee=user,
                employer=user.employer,
                created_by=user,
                document_file=document,
                status_type=status_type,
                effective_date=request.POST.get("immigration_effective_date"),
                expiry_date=request.POST.get("immigration_expiry_date"),
                reference_number=reference,
                notes=notes,
            )
    except ValueError as exc:
        return _reject(str(exc))
    except Exception:
        logger.exception("Immigration upload failed for user %s", user.pk)
        return _reject(
            "The file could not be saved. Your immigration record was not changed."
        )

    messages.success(request, "Immigration document saved. Your tracker is updated.")
    return redirect("employee_immigration_upload")
