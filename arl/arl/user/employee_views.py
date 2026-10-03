import logging
import mimetypes
import os
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_http_methods, require_POST

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


@login_required
def employee_home(request):
    user = request.user
    if getattr(user, "is_company_manager", False):
        return redirect("manager_employee_list")
    signed_documents = SignedDocumentFile.objects.none()
    unsigned_pills = []
    if user.employer_id:
        signed_documents = SignedDocumentFile.objects.filter(
            user=user,
            employer=user.employer,
            is_company_document=False,
        ).order_by("-uploaded_at")
        unsigned_pills = [
            _pill_for_envelope(envelope) for envelope in _unsigned_queryset(user)
        ]
    return render(
        request,
        "user/employee/home.html",
        {
            "signed_documents": signed_documents,
            "unsigned_pills": unsigned_pills,
        },
    )


@login_required
def employee_unsigned_document(request, envelope_id):
    envelope = _envelope_for_actor(request.user, envelope_id)
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
    envelope = _envelope_for_actor(request.user, envelope_id)
    if envelope is None:
        return HttpResponseForbidden("You cannot open this document.")
    try:
        from arl.dsign.helpers import get_recipient_view_url

        signing_url = get_recipient_view_url(
            user=request.user,
            envelope_id=envelope.envelope_id,
            return_url=request.build_absolute_uri(reverse("employee_home")),
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


def _immigration_context(user):
    latest = (
        user.immigration_status_events.filter(is_active=True)
        .order_by("-created_at")
        .first()
    )
    return {
        "status_choices": IMMIGRATION_STATUS_CHOICES,
        "latest_event": latest,
        "permit_expiry": user.work_permit_expiration_date,
        "extension_requested": user.work_permit_extension_requested,
        "extension_date": user.work_permit_extension_date,
    }


@login_required
@require_http_methods(["GET", "POST"])
def employee_immigration_upload(request):
    user = request.user
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
