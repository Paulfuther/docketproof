import json
import logging
import mimetypes
import os
import uuid
from datetime import datetime
from io import BytesIO

from celery.result import AsyncResult
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.contrib.auth.mixins import UserPassesTestMixin
from django.core.paginator import Paginator
from django.db.models import Count, Max, Q
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from docusign_esign import ApiClient, ApiException, TemplatesApi
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from arl.bucket.helpers import upload_to_linode_object_storage
from arl.dsign.helpers import (
    create_docusign_document,
    create_envelope_for_in_app_signing,
    get_access_token,
    get_docusign_envelope,
    get_docusign_template_name_from_template,
    get_recipient_view_url,
    get_template_signature_validation,
)
from arl.dsign.tasks import get_outstanding_docs, list_all_docusign_envelopes_task
from arl.user.models import CustomUser  # adjust import paths
from arl.user.models import Store  # adjust if different
from arl.documentflow.models import ImmigrationStatusEvent
from .forms import EmployeeDocUploadForm, NameEmailForm
from .helpers import get_docusign_edit_url
from .models import DocuSignTemplate, SignedDocumentFile
from .tasks import (
    create_docusign_envelope_task,
    process_company_document_task,
    process_docusign_webhook,
    validate_template_signature_tabs_task,
)
from arl.documentflow.constants import (
    IMMIGRATION_STATUS_TYPES,
    immigration_status_overrides_permit,
)

register_heif_opener()

logger = logging.getLogger(__name__)


def is_member_of_msg_group(user):
    is_member = user.groups.filter(name="docusign").exists()
    if is_member:
        logger.info(f"{user} is a member of 'docusign' group.")
    else:
        logger.info(f"{user} is not a member of 'docusign' group.")
    return is_member


class CreateEnvelopeView(UserPassesTestMixin, View):
    def test_func(self):
        return is_member_of_msg_group(self.request.user)

    def get(self, request):
        form = NameEmailForm(user=request.user)
        return render(request, "dsign/name_email_form.html", {"form": form})

    def post(self, request):
        form = NameEmailForm(request.POST, user=request.user)
        if form.is_valid():
            recipient = form.cleaned_data["recipient"]
            ds_template = form.cleaned_data["template_id"]
            template = form.cleaned_data["template_name"]
            template_name = template.template_name if template else "Unknown Template"

            envelope_args = {
                "user_id": recipient.id,
                "employer_id": recipient.employer_id,
                "signer_email": recipient.email,
                "signer_name": recipient.get_full_name(),
                "template_id": ds_template,
            }

            print("Here are the args:", envelope_args)

            create_docusign_envelope_task.delay(envelope_args)

            messages.success(
                request,
                f"Thank you. The document '{template_name}' has been sent to {recipient.get_full_name()}.",
            )
            return redirect("home")


@csrf_exempt  # In production, use proper CSRF protection.
def docusign_webhook(request):
    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8"))
            process_docusign_webhook.delay(payload)
            return JsonResponse({"message": "Webhook processed successfully"})

        except json.JSONDecodeError:
            logger.error("Invalid JSON payload")
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)

        except Exception as e:
            logger.error(f"Error processing DocuSign webhook: {str(e)}")
            return JsonResponse(
                {"error": f"Error processing DocuSign webhook: {str(e)}"}, status=500
            )

    else:
        return JsonResponse({"error": "Only POST requests are allowed"}, status=405)


def retrieve_docusign_envelope(request):
    try:
        get_docusign_envelope()
        messages.success(request, "Docusign Document Has Been Emailed.")
    except Exception as e:
        messages.error(request, f"Process failed: {str(e)}")

    return redirect("home")


def list_docusign_envelope(request):
    try:
        task_id = request.session.get("list_envelopes_task_id")
        if not task_id:
            # Start the task and save the task ID in the session
            task = list_all_docusign_envelopes_task.delay()
            request.session["list_envelopes_task_id"] = task.id
            logger.info(
                "Started list_all_docusign_envelopes_task with task_id: %s", task.id
            )
            return render(request, "dsign/loading.html")  # Render a loading template

        task_result = AsyncResult(task_id)
        logger.info("Task state: %s", task_result.state)
        print("Task sate: ", task_result.state)
        if task_result.state == "SUCCESS":
            # Task completed successfully
            envelopes = task_result.result
            # Clear the task ID from the session
            del request.session["list_envelopes_task_id"]
            logger.info("Task completed successfully with result: %s", envelopes)
            return render(
                request, "dsign/list_envelopes.html", {"envelopes": envelopes}
            )
        elif task_result.state == "FAILURE":
            # Task failed
            error_message = str(task_result.result)
            # Clear the task ID from the session
            del request.session["list_envelopes_task_id"]
            logger.error("Task failed with error: %s", error_message)
            return render(request, "dsign/error.html", {"error": error_message})
        else:
            # Task is still processing
            logger.info("Task is still processing")
            return render(request, "dsign/loading.html")  # Render a loading template
    except Exception as e:
        logger.error("An error occurred in list_docusign_envelope: %s", str(e))
        return render(request, "dsign/error.html", {"error": str(e)})


def get_docusign_template(request):
    envelope_id = (
        "db908cae-87ac-4c37-aa67-d5a971ee7da7"  # Replace with your envelope_id
    )
    print(envelope_id)
    template_name = get_docusign_template_name_from_template(envelope_id)

    if template_name:
        return JsonResponse({"template_name": template_name})
    else:
        return JsonResponse(
            {"error": "Template not found in the envelope."}, status=404
        )


@login_required
def waiting_for_others_view(request):
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        # Handle the AJAX request to fetch the outstanding documents
        result = get_outstanding_docs.delay()
        envelopes = result.get(timeout=10)  # Adjust the timeout as needed
        return JsonResponse({"outstanding_envelopes": envelopes})
    # Render the template for the initial page load
    return render(request, "dsign/waiting_for_others.html")


@login_required
def create_new_document_page(request):
    """Handles creating a new DocuSign document and saving it in Django."""

    if request.method == "POST":
        template_name = request.POST.get("template_name", "New Document")
        print("Template Name :", template_name)
        template_id = create_docusign_document(
            user=request.user, template_name=template_name
        )

        if template_id:
            messages.success(
                request, f"Document '{template_name}' created successfully!"
            )
            # ✅ Get the edit URL for the new template
            edit_url = get_docusign_edit_url(template_id)

            if edit_url:
                return redirect(edit_url)  # ✅ Immediately open the DocuSign editor
            else:
                messages.error(
                    request, "Could not retrieve the edit URL. Please try manually."
                )
        else:
            messages.error(request, "Failed to create a new document.")

        return redirect("hr_dashboard")  # Redirect to HR dashboard

    return render(request, "dsign/create_document.html")


@login_required
def edit_document_page(request, template_id):
    """Shows helper info, then lets user continue to DocuSign editor."""
    edit_url = get_docusign_edit_url(template_id)
    template = DocuSignTemplate.objects.filter(template_id=template_id).first()

    # ✅ Run the helper directly (sync)
    validation_result = get_template_signature_validation(template_id)
    has_sign_here_tab = validation_result.get("has_sign_here", False)

    return render(
        request,
        "dsign/dsign_redirect.html",
        {
            "edit_url": edit_url,
            "template": template,
            "has_sign_here_tab": has_sign_here_tab,
        },
    )


def check_docusign_template_ready(template_id):
    access_token = get_access_token().access_token
    base_path = settings.DOCUSIGN_BASE_PATH
    account_id = settings.DOCUSIGN_ACCOUNT_ID
    api_client = ApiClient()
    api_client.host = base_path
    api_client.set_default_header("Authorization", f"Bearer {access_token}")

    templates_api = TemplatesApi(api_client)

    try:
        template = templates_api.get(account_id, template_id)

        # ✅ Basic checks
        has_docs = bool(template.documents)
        has_subject = bool(template.email_subject and template.email_subject.strip())
        has_signers = bool(template.recipients and template.recipients.signers)

        if not (has_docs and has_subject and has_signers):
            print("❌ Missing docs, subject, or signers.")
            return False

        gsa_signed = False

        for signer in template.recipients.signers:
            role = signer.role_name
            # We don’t call .list_tabs anymore because that API doesn't work for templates
            has_tabs = (
                hasattr(signer, "tabs")
                and signer.tabs
                and (
                    getattr(signer.tabs, "sign_here_tabs", None)
                    or getattr(signer.tabs, "signHereTabs", None)
                )
            )

            print(f"🔍 {role} tabs: {signer.tabs}")

            if has_tabs:
                print(f"✅ {role} appears to have SignHere tabs.")
                if role == "GSA":
                    gsa_signed = True
            else:
                print(f"❌ {role} has no apparent tabs assigned.")

        if not gsa_signed:
            print("❌ GSA role is missing required signature tab.")
            return False

        return True

    except ApiException as e:
        print(f"❌ Error retrieving template: {e}")
        return False


def update_template_readiness(template, validation_passed: bool):
    """
    Updates the template readiness flag based on validation outcome.

    Args:
        template (DocuSignTemplate): the template to update
        validation_passed (bool): result from validate_signature_roles
    """
    # Ensure required fields are present
    required_fields_present = all(
        [
            template.template_name,
            template.template_id,
            template.signer_role,  # This should be 'GSA'
        ]
    )

    template.is_ready_to_send = required_fields_present and validation_passed
    template.save(update_fields=["is_ready_to_send"])


def docusign_close(request):
    template_id = request.GET.get("template_id")
    template = None
    status = "validating"

    if template_id:
        template = DocuSignTemplate.objects.filter(template_id=template_id).first()

        if template:
            # Kick off validation in background
            validate_template_signature_tabs_task.delay(template_id)
        else:
            status = "notfound"

    return render(
        request,
        "dsign/template_edit_success.html",
        {
            "template": template,
            "status": status,
        },
    )


@login_required
def set_new_hire_template(request, template_id):
    template = get_object_or_404(
        DocuSignTemplate,
        id=template_id,
        employer=request.user.employer,
    )

    if request.method == "POST":
        # ✅ If New Hire checkbox is checked, unset others first
        if "is_new_hire_template" in request.POST:
            # Unset all other templates first
            DocuSignTemplate.objects.filter(
                employer=request.user.employer, is_new_hire_template=True
            ).update(is_new_hire_template=False)
            template.is_new_hire_template = True
        else:
            template.is_new_hire_template = False

        # ✅ In-App Signing is independent
        template.is_in_app_signing_template = (
            "is_in_app_signing_template" in request.POST
        )
        # Handle company document toggle
        if request.POST.get("is_company_document"):
            template.is_company_document = True
        else:
            template.is_company_document = False

        template.save()
        messages.success(request, "Template updated successfully.")

    return redirect("hr_dashboard")


# Bulk upload of forms to S3 bucket
@user_passes_test(lambda u: u.is_superuser)
def bulk_upload_signed_documents_view(request):
    if request.method == "POST":
        form = EmployeeDocUploadForm(request.POST)
        if form.is_valid():
            user = form.cleaned_data["user"]
            employer = form.cleaned_data["employer"]
            files = request.FILES.getlist("files")

            folder = uuid.uuid4().hex[:8]
            upload_count = 0

            for uploaded_file in files:
                file_path = f"DOCUMENTS/{employer.name.replace(' ', '_')}/{folder}/{uploaded_file.name}"
                upload_to_linode_object_storage(uploaded_file, file_path)

                SignedDocumentFile.objects.create(
                    user=user,
                    employer=employer,
                    envelope_id=uuid.uuid4().hex[:10],
                    file_name=uploaded_file.name,
                    file_path=file_path,
                )
                upload_count += 1

            messages.success(request, f"Successfully uploaded {upload_count} file(s).")
            return redirect("bulk_upload_signed_documents")

    else:
        form = EmployeeDocUploadForm()

    return render(request, "dsign/bulk_upload.html", {"form": form})


@login_required
def in_app_signing_dashboard(request):
    employer = request.user.employer

    # Get all templates marked for in-app signing
    templates = DocuSignTemplate.objects.filter(
        employer=employer,
        is_in_app_signing_template=True,
        is_ready_to_send=True,  # Assuming you want only finalized forms
    ).order_by("template_name")

    context = {
        "templates": templates,
    }
    return render(request, "dsign/in_app_signing_dashboard.html", context)


@login_required
def start_in_app_signing(request, template_id):
    template = get_object_or_404(
        DocuSignTemplate,
        id=template_id,
        employer=request.user.employer,
        is_in_app_signing_template=True,
    )

    envelope_id = create_envelope_for_in_app_signing(
        user=request.user,
        template_id=template.template_id,
        employer=request.user.employer,
    )

    # Build Return URL dynamically
    return_url = f"{settings.SITE_URL}/hr/dashboard/"

    # Get the Signing URL
    signing_url = get_recipient_view_url(
        user=request.user,
        envelope_id=envelope_id,
        return_url=return_url,
    )

    return redirect(signing_url)


MAX_EACH_BYTES = 25 * 1024 * 1024  # 25 MB per file (tune as needed)
ALLOWED_EXTS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".heic",
    ".heif",
    ".txt",
    ".csv",
    ".rtf",
    ".webp",
}


ALLOWED_UPLOAD_CT = {
    # docs
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
    "text/csv",
    "application/rtf",
    # images (we’ll normalize HEIC to JPG for safety)
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    # some Androids use octet-stream for images; we’ll accept and probe
    "application/octet-stream",
}

MAX_UPLOAD_MB = 25


def _immigration_validation_message(exc):
    if getattr(exc, "message_dict", None):
        parts = []
        for field_messages in exc.message_dict.values():
            parts.extend(str(message) for message in field_messages)
        if parts:
            return " ".join(parts)
    if getattr(exc, "messages", None):
        return " ".join(str(message) for message in exc.messages)
    return "Immigration status was not saved. Add the effective date and a document or reference number."


def _guess_ct(uploaded):
    # Some Androids use octet-stream—fallback to filename
    ct = getattr(uploaded, "content_type", None) or ""
    if ct == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(uploaded.name)
        return guessed or ct
    return ct


@login_required
def upload_employee_documents(request):
    if request.method != "POST":
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    employer = getattr(request.user, "employer", None)
    if not employer:
        messages.error(request, "You must belong to an employer to upload documents.")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    # Pull form fields
    user_id = request.POST.get("user_id")
    title = (request.POST.get("document_title") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    fileobj = request.FILES.get("file")
    print("file obj :", fileobj)

    # Detect immigration status updates in the docuents
    immigration_status_type = (request.POST.get("immigration_status_type") or "").strip()
    immigration_effective_date = request.POST.get("immigration_effective_date") or None
    immigration_expiry_date = request.POST.get("immigration_expiry_date") or None
    immigration_reference_number = (
        request.POST.get("immigration_reference_number") or ""
    ).strip()

    if immigration_status_type and immigration_status_type not in IMMIGRATION_STATUS_TYPES:
        messages.error(request, "That immigration status is not a valid choice.")
        return redirect(reverse("documents_dashboard") + "?employee")

    if immigration_status_type and immigration_status_overrides_permit(
        immigration_status_type
    ):
        if not immigration_effective_date:
            messages.error(
                request,
                "Effective date is required when the immigration status "
                "changes work-permit ranking.",
            )
            return redirect(reverse("documents_dashboard") + "?employee")
        try:
            datetime.strptime(immigration_effective_date, "%Y-%m-%d")
        except ValueError:
            messages.error(request, "Effective date must be a real date.")
            return redirect(reverse("documents_dashboard") + "?employee")

    # Basic validations
    if not user_id or not title or not fileobj:
        messages.error(request, "Employee, title, and file are required.")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    # Employee must be within the same employer
    employee = get_object_or_404(
        CustomUser, pk=user_id, employer=employer, is_active=True
    )

    # Size check
    size_mb = fileobj.size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        messages.error(
            request, f"File too large ({size_mb:.1f} MB). Max {MAX_UPLOAD_MB} MB."
        )
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    # Content-type sanity (with a smarter guess for octet-stream)
    ct = _guess_ct(fileobj)
    if ct not in ALLOWED_UPLOAD_CT:
        messages.error(request, f"Unsupported file type: {ct or 'unknown'}")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    # Build path pieces
    company = slugify(employer.name or "company")
    today = timezone.now()
    year = today.strftime("%Y")
    month = today.strftime("%m")
    base, ext = os.path.splitext(fileobj.name or "")
    ext = (ext or "").lower()

    # Decide if we should normalize to JPG
    should_normalize_to_jpg = False
    if ct in {"image/heic", "image/heif"} or ext in {".heic", ".heif"}:
        should_normalize_to_jpg = True
    elif ct == "application/octet-stream" and ext in {".heic", ".heif"}:
        should_normalize_to_jpg = True

    safe_title = slugify(title) or "document"
    unique_id = uuid.uuid4().hex[:8]

    # IMPORTANT: start with original ext (may be empty)
    final_ext = ext
    upload_buf = None

    try:
        if should_normalize_to_jpg:
            # Convert HEIC/HEIF -> JPEG (with EXIF fix + resize cap)
            img = Image.open(fileobj)
            try:
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.thumbnail((1500, 1500), Image.LANCZOS)

            out = BytesIO()
            img.save(out, format="JPEG", quality=85, optimize=True)
            out.seek(0)
            upload_buf = out
            final_ext = ".jpg"
        else:
            # Non-images: upload as-is
            upload_buf = fileobj

            # If no extension, pick one based on content-type
            if not final_ext:
                if ct == "application/pdf":
                    final_ext = ".pdf"
                elif ct == "application/msword":
                    final_ext = ".doc"
                elif (
                    ct
                    == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ):
                    final_ext = ".docx"
                elif ct == "application/vnd.ms-excel":
                    final_ext = ".xls"
                elif (
                    ct
                    == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ):
                    final_ext = ".xlsx"
                elif ct == "text/plain":
                    final_ext = ".txt"
                elif ct == "text/csv":
                    final_ext = ".csv"
                elif ct.startswith("image/"):
                    final_ext = ".jpg"  # last resort for unknown image types
                else:
                    final_ext = ".bin"
    except Exception as e:
        messages.error(request, f"Could not process file: {e}")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    filename = f"{safe_title}-{unique_id}{final_ext}"
    object_key = f"DOCUMENTS/{company}/{year}/{month}/{employee.id}/{filename}"

    try:
        upload_to_linode_object_storage(upload_buf, object_key)
    finally:
        if isinstance(upload_buf, BytesIO):
            upload_buf.close()

    uploaded_doc = SignedDocumentFile.objects.create(
        user=employee,
        employer=employer,
        envelope_id=uuid.uuid4().hex[:10],
        file_name=filename,
        file_path=object_key,
        document_title=title,
        notes=notes,
        is_company_document=False,
    )

    if immigration_status_type:
        try:
            ImmigrationStatusEvent.objects.create(
                user=employee,
                employer=employer,
                status_type=immigration_status_type,
                effective_date=immigration_effective_date,
                expiry_date=immigration_expiry_date,
                reference_number=immigration_reference_number,
                notes=notes,
                document_file=uploaded_doc,
                created_by=request.user,
                is_active=True,
            )
        except ValidationError as exc:
            messages.error(request, _immigration_validation_message(exc))
            return redirect(reverse("documents_dashboard") + "?employee")

    messages.success(
        request, f"Uploaded '{title}' for {employee.get_full_name() or employee.email}."
    )
    return redirect(reverse("documents_dashboard") + "?employee")


@login_required
def upload_store_documents(request):
    if request.method != "POST":
        return redirect(
            reverse("hr_dashboard") + "?tab=document_center"
        )  # or wherever you want

    employer = getattr(request.user, "employer", None)
    if not employer:
        messages.error(request, "You must belong to an employer to upload documents.")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    store_id = request.POST.get("store_id")
    title = (request.POST.get("document_title") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    fileobj = request.FILES.get("file")

    if not store_id or not title or not fileobj:
        messages.error(request, "Store, title, and file are required.")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    store = get_object_or_404(Store, pk=store_id, employer=employer)

    # --- size/type checks (reuse your constants/helpers) ---
    size_mb = fileobj.size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        messages.error(
            request, f"File too large ({size_mb:.1f} MB). Max {MAX_UPLOAD_MB} MB."
        )
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    ct = getattr(fileobj, "content_type", None)
    if ct not in ALLOWED_UPLOAD_CT:
        messages.error(request, f"Unsupported file type: {ct or 'unknown'}")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    # --- prepare filename/path ---
    company = slugify(employer.name or "company")
    today = timezone.now()
    year, month = today.strftime("%Y"), today.strftime("%m")

    base, ext = os.path.splitext(fileobj.name or "")
    ext = (ext or "").lower()

    # Image normalization (HEIC/HEIF -> JPEG)
    should_normalize_to_jpg = False
    if ct in {"image/heic", "image/heif"} or ext in {".heic", ".heif"}:
        should_normalize_to_jpg = True
    elif ct == "application/octet-stream" and ext in {".heic", ".heif"}:
        should_normalize_to_jpg = True

    safe_title = slugify(title) or "document"
    unique_id = uuid.uuid4().hex[:8]
    final_ext = ext or ".bin"

    try:
        if should_normalize_to_jpg:
            im = Image.open(fileobj).convert("RGB")
            out = BytesIO()
            im.save(out, format="JPEG", quality=85, optimize=True)
            out.seek(0)
            upload_buf = out
            final_ext = ".jpg"
        else:
            upload_buf = fileobj
            if not ext:
                if ct == "application/pdf":
                    final_ext = ".pdf"
                elif ct in {"text/plain", "text/csv"}:
                    final_ext = ".txt" if ct == "text/plain" else ".csv"
                elif ct and ct.startswith("image/"):
                    final_ext = ".jpg"
                else:
                    final_ext = ".bin"
    except Exception as e:
        messages.error(request, f"Could not process file: {e}")
        return redirect(reverse("hr_dashboard") + "?tab=document_center")

    filename = f"{safe_title}-{unique_id}{final_ext}"
    # Foldering by store for easy browsing later
    object_key = f"DOCUMENTSTEST/{company}/stores/{store.number or store.id}/{year}/{month}/{filename}"

    try:
        upload_to_linode_object_storage(upload_buf, object_key)
    finally:
        if isinstance(upload_buf, BytesIO):
            upload_buf.close()

    SignedDocumentFile.objects.create(
        user=None,
        store=store,
        employer=employer,
        envelope_id=uuid.uuid4().hex[:10],
        file_name=filename,
        file_path=object_key,
        document_title=title,
        notes=notes,
        is_company_document=True,  # ← Use this to indicate non-employee doc
    )

    messages.success(
        request, f"Uploaded '{title}' for store {store.number or store.name}."
    )
    return redirect(reverse("hr_dashboard") + "?tab=document_center")


@login_required
def documents_dashboard(request):
    print("DOCUMENTS DASHBOARD VIEW HIT")
    employer = getattr(request.user, "employer", None)
    if not employer:
        return redirect("home")

    # ✅ Restrict access to Managers & Employers only
    if (
        not employer
        or not request.user.groups.filter(name__in=["Manager", "EMPLOYER"]).exists()
    ):
        messages.error(request, "You must be an employer or manager to view documents.")
        return redirect("home")

    active_tab = request.GET.get("tab", "employee")  # employee|store|company

    # base data for upload selects
    employees = CustomUser.objects.filter(employer=employer, is_active=True).order_by(
        "first_name", "last_name"
    )

    stores = Store.objects.filter(employer=employer).order_by("number")

    # quick recent lists (first paint)
    # --- NEW: last 5 employees who uploaded something most recently
    # find the latest upload per user, order by that, take 5
    latest_per_user = (
        SignedDocumentFile.objects.filter(employer=employer, user__isnull=False)
        .values("user")
        .annotate(last_upload=Max("uploaded_at"))
        .order_by("-last_upload")[:30]
    )

    user_ids = [row["user"] for row in latest_per_user]

    recent_employee_heads = (
        CustomUser.objects.filter(id__in=user_ids)
        .annotate(
            # NOTE: use the correct reverse name "signed_documents"
            last_upload=Max(
                "signed_documents__uploaded_at",
                filter=Q(signed_documents__employer=employer),
            ),
            doc_count=Count(
                "signed_documents",
                filter=Q(signed_documents__employer=employer),
                destinch=True,
            ),
        )
        .order_by("-last_upload")
    )

    # ------------------------------
    # Paginated full employee docs
    # ------------------------------
    employee_docs_qs = (
        SignedDocumentFile.objects.filter(employer=employer, user__isnull=False)
        .select_related("user")
        .order_by("-uploaded_at")
    )

    page_number = request.GET.get("page", 1)
    paginator = Paginator(employee_docs_qs, 10)  # 10 per page
    page_obj = paginator.get_page(page_number)

    # ------------------------------
    # Paginated store docs (10 per page)
    # ------------------------------
    store_docs_qs = (
        SignedDocumentFile.objects.filter(employer=employer, store__isnull=False)
        .select_related("store")
        .order_by("-uploaded_at")
    )

    store_page_number = request.GET.get("store_page", 1)
    store_paginator = Paginator(store_docs_qs, 10)  # 10 per page
    recent_store_docs = store_paginator.get_page(store_page_number)

    # print("store docs :", recent_store_docs)

    # ------------------------------
    # Paginated company docs (10 per page)
    # ------------------------------
    company_docs_qs = SignedDocumentFile.objects.filter(
        employer=employer, is_company_document=True
    ).order_by("-uploaded_at")

    company_page_number = request.GET.get("company_page", 1)
    company_paginator = Paginator(company_docs_qs, 10)
    recent_company_docs = company_paginator.get_page(company_page_number)

    # print(recent_company_docs)

    return render(
        request,
        "user/documents/dashboard.html",
        {
            "IMMIGRATION_STATUS_TYPES": IMMIGRATION_STATUS_TYPES,
            "active_tab": active_tab,
            "employees": employees,
            "stores": stores,
            "recent_employee_heads": recent_employee_heads,
            "employee_documents": page_obj,
            "recent_store_docs": recent_store_docs,
            "company_results": recent_company_docs,
            "q": "",
        },
    )


# ---------- HTMX searches ----------
@login_required
def employee_docs_search(request):
    employer = getattr(request.user, "employer", None)
    if not employer:
        return render(
            request,
            "user/documents/partials/employee_results.html",
            {"doc_q": "", "employees_results": [], "employee_documents": []},
        )

    q = (request.GET.get("doc_q") or "").strip()
    include_inactive = request.GET.get("include_inactive") == "1"

    # ✅ IMPORTANT: if q is empty, return "Type..." UI (don’t match everyone)
    if not q:
        return render(
            request,
            "user/documents/partials/employee_results.html",
            {"doc_q": "", "employees_results": [], "employee_documents": []},
        )

    qs = CustomUser.objects.filter(employer=employer)
    if not include_inactive:
        qs = qs.filter(is_active=True)

    employees_results = (
        qs.filter(
            Q(first_name__icontains=q)
            | Q(last_name__icontains=q)
            | Q(email__icontains=q)
        )
        .annotate(
            doc_count=Count(
                "signed_documents",
                filter=Q(signed_documents__employer=employer),
                distinct=True,
            )
        )
        .order_by("-is_active", "first_name", "last_name")
    )

    docs = (
        SignedDocumentFile.objects.filter(employer=employer, user__in=employees_results)
        .select_related("user")
        .order_by("-uploaded_at")
    )

    return render(
        request,
        "user/documents/partials/employee_results.html",
        {
            "doc_q": q,
            "employees_results": employees_results,
            "employee_documents": docs,
        },
    )


@login_required
def employee_docs_panel(request, user_id):
    employer = getattr(request.user, "employer", None)
    if not employer:
        return redirect("home")

    # optional safety: ensure user belongs to employer
    user = CustomUser.objects.filter(id=user_id, employer=employer).first()
    if not user:
        return render(
            request,
            "user/documents/partials/employee_docs_panel.html",
            {"docs_page": None, "user_id": user_id, "error": "Employee not found"},
        )

    qs = SignedDocumentFile.objects.filter(employer=employer, user_id=user_id).order_by(
        "-uploaded_at"
    )

    page_number = request.GET.get("page", 1)
    paginator = Paginator(qs, 10)
    docs_page = paginator.get_page(page_number)

    return render(
        request,
        "user/documents/partials/employee_docs_panel.html",
        {"docs_page": docs_page, "user_id": user_id},
    )


@login_required
def company_docs_search(request):
    employer = getattr(request.user, "employer", None)
    q = (request.GET.get("q") or "").strip()

    qs = SignedDocumentFile.objects.filter(employer=employer, is_company_document=True)
    # print("q :", q, "qs :", qs)
    if q:
        qs = qs.filter(
            Q(document_title__icontains=q)
            | Q(file_name__icontains=q)
            | Q(notes__icontains=q)
        )
    company_documents = qs.order_by("-uploaded_at")[:50]
    # print(q, company_documents)

    return render(
        request,
        "user/documents/partials/company_results.html",
        {"company_results": company_documents, "q": q},
    )


@login_required
def store_docs_search(request):
    employer = getattr(request.user, "employer", None)
    if not employer:
        return HttpResponse("No employer found.", status=400)

    q = (request.GET.get("q") or "").strip()

    docs = SignedDocumentFile.objects.filter(
        employer=employer,
        store__isnull=False,
    ).select_related("store", "user")

    if q:
        docs = docs.filter(store__number__icontains=q)
    else:
        docs = docs.none()

    docs = docs.order_by("-uploaded_at")

    return render(
        request,
        "user/documents/partials/store_results.html",
        {"docs": docs, "q": q},
    )


MAX_BYTES = 100 * 1024 * 1024  # 100 MB


def _human_size(n: int) -> str:
    # same helper you used for stores
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def _save_to_temp(uploaded, suffix: str = "") -> str:
    from tempfile import NamedTemporaryFile

    from django.core.files.uploadedfile import InMemoryUploadedFile

    ext = suffix or (os.path.splitext(getattr(uploaded, "name", ""))[1] or ".bin")
    tmp = NamedTemporaryFile(
        delete=False, dir=getattr(settings, "MEDIA_TMP", None), suffix=ext
    )
    if isinstance(uploaded, InMemoryUploadedFile):
        tmp.write(uploaded.read())
    else:
        for chunk in uploaded.chunks():
            tmp.write(chunk)
    tmp.flush()
    tmp.close()
    return tmp.name


@login_required
def upload_company_documents_async(request):
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    employer = getattr(request.user, "employer", None)
    if not employer:
        return JsonResponse({"ok": False, "error": "No employer"}, status=403)

    title = (request.POST.get("document_title") or "").strip()
    notes = (request.POST.get("notes") or "").strip()
    fileobj = request.FILES.get("file")

    if not title or not fileobj:
        return JsonResponse(
            {"ok": False, "error": "Title and file are required."}, status=400
        )

    if getattr(fileobj, "size", 0) > MAX_BYTES:
        return JsonResponse(
            {
                "ok": False,
                "error": f"File too large (max {MAX_BYTES // (1024 * 1024)}MB).",
            },
            status=400,
        )

    # Build S3 object key (company docs folder)
    company = slugify(employer.name or "company")
    year, month = timezone.now().strftime("%Y"), timezone.now().strftime("%m")
    base, ext = os.path.splitext(fileobj.name or "")
    safe_title = slugify(title) or "document"
    unique_id = uuid.uuid4().hex[:8]
    final_ext = ext or ".bin"
    size_label = _human_size(fileobj.size)
    filename = f"{safe_title}-{size_label}-{unique_id}{final_ext}"
    object_key = f"DOCUMENTS/{company}/company/{year}/{month}/{filename}"

    sdf = SignedDocumentFile.objects.create(
        user=None,
        store=None,
        employer=employer,
        envelope_id=uuid.uuid4().hex[:10],
        file_name=filename,
        file_path=object_key,
        document_title=title,
        notes=notes,
        is_company_document=True,
    )

    # Save temp & enqueue task
    tmp_path = _save_to_temp(fileobj, suffix=final_ext)

    process_company_document_task.delay(str(sdf.id), tmp_path, object_key)

    # HTMX: fire event so UI refreshes without navigation
    resp = JsonResponse({"ok": True, "doc_id": sdf.id}, status=202)
    resp["HX-Trigger"] = json.dumps({"companyUploadQueued": {"doc_id": sdf.id}})
    return resp

    # ---   Notes edit section for Company Documents -- #


@login_required
def company_doc_notes_edit(request, doc_id):
    employer = getattr(request.user, "employer", None)
    doc = get_object_or_404(SignedDocumentFile, id=doc_id, employer=employer)

    return render(
        request, "user/documents/partials/company_notes_form.html", {"d": doc}
    )


@login_required
def company_doc_notes_save(request, doc_id):
    employer = getattr(request.user, "employer", None)
    doc = get_object_or_404(SignedDocumentFile, id=doc_id, employer=employer)

    if request.method == "POST":
        doc.notes = request.POST.get("notes", "").strip()
        doc.save(update_fields=["notes"])

        return render(
            request,
            "user/documents/partials/company_notes_block.html",
            {"d": doc, "open_notes": True},  # 👈 keep open after save
        )

    return HttpResponse(status=405)


@login_required
def company_doc_notes_edit_mobile(request, doc_id):
    employer = getattr(request.user, "employer", None)
    doc = get_object_or_404(SignedDocumentFile, id=doc_id, employer=employer)
    return render(
        request, "user/documents/partials/company_notes_form_mobile.html", {"d": doc}
    )


@login_required
def company_doc_notes_cancel(request, doc_id):
    employer = getattr(request.user, "employer", None)
    doc = get_object_or_404(SignedDocumentFile, id=doc_id, employer=employer)
    return render(
        request, "user/documents/partials/company_notes_block.html", {"d": doc}
    )


@login_required
def company_doc_notes_save_mobile(request, doc_id):
    employer = getattr(request.user, "employer", None)
    doc = get_object_or_404(SignedDocumentFile, id=doc_id, employer=employer)

    if request.method == "POST":
        doc.notes = request.POST.get("notes", "").strip()
        doc.save(update_fields=["notes"])
        return render(
            request,
            "user/documents/partials/company_notes_block_mobile.html",
            {"d": doc, "open_notes": True},
        )
    return HttpResponse(status=405)


@login_required
def company_doc_notes_cancel_mobile(request, doc_id):
    employer = getattr(request.user, "employer", None)
    doc = get_object_or_404(SignedDocumentFile, id=doc_id, employer=employer)
    return render(
        request, "user/documents/partials/company_notes_block_mobile.html", {"d": doc}
    )
