import logging
import os
import uuid
from io import BytesIO
from uuid import uuid4

import pytz
from arl.helpers import (
    get_s3_images_for_salt_log,
    get_signed_url_for_key,
    upload_to_linode_object_storage,
)
from arl.user.models import Store
from arl.utils.images import normalize_to_jpeg
from celery.result import AsyncResult
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.text import slugify
from django.views import View
from PIL import Image, ImageOps, UnidentifiedImageError
from waffle import flag_is_active

from .forms import (
    AnswerFormSet,
    ChecklistForm,
    ChecklistItemFormSet,
    ChecklistTemplateForm,
    QuestionFormSet,
    QuizForm,
    SaltLogForm,
    TemplateItemFormSet,
)
from .action_plan_delivery import checklist_splits_action_plan
from .models import Checklist, ChecklistItem, ChecklistTemplate, Quiz, SaltLog
from .store_address import store_report_context
from .tasks import (
    generate_checklist_pdf_task,
    generate_fresh_checklist_pdf,
    generate_salt_log_pdf_task,
)

logger = logging.getLogger(__name__)


@login_required
def create_quiz(request):
    if request.method == "POST":
        quiz_form = QuizForm(request.POST)
        question_formset = QuestionFormSet(request.POST, instance=Quiz())

        if quiz_form.is_valid():
            quiz = quiz_form.save()

            # Reinitialize the formset with the saved quiz instance
            question_formset = QuestionFormSet(request.POST, instance=quiz)
            if question_formset.is_valid():
                questions = question_formset.save(commit=False)
                for question in questions:
                    question.quiz = quiz
                    question.save()

                    # Handle the answers for each question
                    answer_formset = AnswerFormSet(
                        request.POST,
                        instance=question,
                        prefix=f"'questions-{question.id}')",
                    )
                    if answer_formset.is_valid():
                        answer_formset.save()
                    else:
                        print(
                            f"Answer formset errors for question{question.id}:",
                            answer_formset.errors,
                        )

                return redirect("quiz_list")
            else:
                print("Question formset errors:", question_formset.errors)
        else:
            print("Quiz form errors:", quiz_form.errors)
    else:
        quiz_form = QuizForm()
        question_formset = QuestionFormSet(instance=Quiz())

    return render(
        request,
        "quiz/create_quiz.html",
        {
            "quiz_form": quiz_form,
            "question_formset": question_formset,
        },
    )


@login_required
# @waffle_flag('quiz')
def quiz_list(request):
    # Check if the 'quiz' feature flag is active
    if not flag_is_active(request, "quiz"):
        # If not active, render the custom 403 error template
        return render(request, "flags/feature_not_available.html", status=403)
    quizzes = Quiz.objects.all()
    return render(request, "quiz/quiz_list.html", {"quizzes": quizzes})


@login_required
def take_quiz(request, quiz_id):
    # Check if the 'quiz' feature flag is active
    if not flag_is_active(request, "quiz"):
        # If not active, render the custom 403 error template

        return render(request, "flags/feature_not_available.html", status=403)
    quiz = get_object_or_404(Quiz, pk=quiz_id)
    questions = quiz.questions.all()

    if request.method == "POST":
        score = 0
        total_questions = questions.count()

        for question in questions:
            selected_answer = request.POST.get(f"question_{question.id}")
            correct_answer = question.answers.filter(is_correct=True).first()

            # Assuming correct_answer.text is either "yes" or "no"
            if correct_answer and selected_answer == correct_answer.text.lower():
                score += 1

        return render(
            request,
            "quiz/quiz_result.html",
            {"quiz": quiz, "score": score, "total_questions": total_questions},
        )

    return render(
        request, "quiz/take_quiz.html", {"quiz": quiz, "questions": questions}
    )


# Photos stay under the historical Linode prefix SALTLOG/{employer}/{folder}/.
# Dropzone posts salt_log (the row pk). The client folder name is ignored.
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
    "application/octet-stream",
    "",
}
MAX_SALT_IMAGE_BYTES = 8 * 1024 * 1024
EASTERN = pytz.timezone("America/New_York")


def _sanitize_folder(value: str) -> str:
    value = (value or "").strip().strip("/").replace("..", "")
    return "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_", "/"))


def salt_logs_for_user(user):
    employer = getattr(user, "employer", None)
    if employer is None:
        return SaltLog.objects.none()
    return SaltLog.objects.filter(user_employer=employer)


def _stores_for_user(user):
    employer = getattr(user, "employer", None)
    if employer is None:
        return Store.objects.none()
    return Store.objects.filter(employer=employer).order_by("number")


def _eastern_now():
    current = timezone.now().astimezone(EASTERN)
    return current.date(), current.time().replace(microsecond=0)


# Same page size as the SMS activity list. Checklist lists use 20.
SALT_LOG_PAGE_SIZE = 25
SALT_LOG_TABS = ("drafts", "submitted", "completed")


def _salt_log_page(request, queryset, page_param):
    """One page of salt logs. Invalid pages fall back to the nearest real page."""
    paginator = Paginator(queryset, SALT_LOG_PAGE_SIZE)
    return paginator.get_page(request.GET.get(page_param) or 1)


@login_required
def salt_log_dashboard(request):
    """Open drafts, submitted, and completed salt logs for this employer.

    Each tab is its own page of rows. Counts stay on the badges; the tables
    only load the current page.
    """
    q = (request.GET.get("q") or "").strip()
    store_filter = (request.GET.get("store") or "").strip()
    active_tab = (request.GET.get("tab") or "drafts").strip()
    if active_tab not in SALT_LOG_TABS:
        active_tab = "drafts"

    logs = (
        salt_logs_for_user(request.user)
        .select_related("store", "user", "submitted_by")
        .order_by("-hidden_timestamp", "-pk")
    )
    if q:
        text = Q(area_salted__icontains=q)
        if q.isdigit():
            text |= Q(store__number=int(q))
        logs = logs.filter(text)
    if store_filter.isdigit():
        logs = logs.filter(store_id=int(store_filter))

    return render(
        request,
        "quiz/salt_log_list.html",
        {
            "q": q,
            "store_filter": store_filter,
            "active_tab": active_tab,
            "stores": _stores_for_user(request.user),
            "drafts": _salt_log_page(
                request, logs.filter(status=SaltLog.STATUS_DRAFT), "drafts_page"
            ),
            "submitted": _salt_log_page(
                request,
                logs.filter(status=SaltLog.STATUS_SUBMITTED),
                "submitted_page",
            ),
            "completed": _salt_log_page(
                request,
                logs.filter(status=SaltLog.STATUS_COMPLETED),
                "completed_page",
            ),
        },
    )


@login_required
def salt_log_start(request):
    """Create the draft in this request so photos have a row to attach to."""
    employer = getattr(request.user, "employer", None)
    if employer is None:
        messages.error(
            request, "Your account has no company. Ask an admin to set one."
        )
        return redirect("salt_log_list")

    date_salted, time_salted = _eastern_now()
    salt_log = SaltLog(
        user=request.user,
        user_employer=employer,
        status=SaltLog.STATUS_DRAFT,
        date_salted=date_salted,
        time_salted=time_salted,
    )
    salt_log.ensure_image_folder()
    salt_log.save()
    return redirect("salt_log_edit", pk=salt_log.pk)


def _salt_log_images(salt_log):
    if not salt_log.image_folder or not salt_log.user_employer_id:
        return []
    return (
        get_s3_images_for_salt_log(salt_log.image_folder, salt_log.user_employer) or []
    )


def _lock_salt_form(form):
    for field in form.fields.values():
        field.disabled = True


@login_required
def salt_log_edit(request, pk):
    salt_log = get_object_or_404(salt_logs_for_user(request.user), pk=pk)
    editable = salt_log.status == SaltLog.STATUS_DRAFT

    if request.method == "POST":
        is_autosave = request.GET.get("autosave") == "1"
        action = "save" if is_autosave else (request.POST.get("action") or "save")
        if not editable:
            if is_autosave:
                return JsonResponse(
                    {"ok": False, "error": "Already submitted"}, status=409
                )
            messages.error(request, "This salt log is already submitted.")
            return redirect("salt_log_list")

        form = SaltLogForm(
            request.POST,
            instance=salt_log,
            user=request.user,
            validate_submit=(not is_autosave) and action == "submit",
        )
        if form.is_valid():
            saved = form.save(commit=False)
            if not saved.user_id:
                saved.user = request.user
            saved.user_employer = request.user.employer
            saved.ensure_image_folder()
            if action == "submit":
                saved.status = SaltLog.STATUS_SUBMITTED
                saved.submitted_by = request.user
                saved.submitted_at = timezone.now()
            saved.save()

            if is_autosave:
                return JsonResponse(
                    {"ok": True, "saved_at": timezone.now().isoformat()}
                )

            if action == "submit":
                try:
                    generate_salt_log_pdf_task.delay(saved.id)
                    messages.success(
                        request, "Salt log submitted. PDF generation started."
                    )
                except Exception as exc:
                    messages.error(
                        request,
                        f"Salt log submitted, but PDF task failed to queue: {exc}",
                    )
                return redirect("salt_log_list")

            messages.success(request, "Draft saved.")
            return redirect("salt_log_edit", pk=saved.pk)

        if is_autosave:
            return JsonResponse({"ok": False, "errors": form.errors}, status=422)
    else:
        form = SaltLogForm(instance=salt_log, user=request.user)

    if not editable:
        _lock_salt_form(form)

    levels_value = form["levels_ok"].value()
    show_exception = levels_value in ("no", False) or bool(
        form["exception_what"].errors
        or form["exception_who"].errors
        or form["exception_when"].errors
    )
    return render(
        request,
        "quiz/salt_log_edit.html",
        {
            "salt_log": salt_log,
            "form": form,
            "editable": editable,
            "existing_images": _salt_log_images(salt_log),
            "show_exception": show_exception,
        },
    )


class ProcessSaltLogImagesView(LoginRequiredMixin, View):
    """Dropzone endpoint. The salt log row must already exist."""

    login_url = "/login/"

    def post(self, request, *args, **kwargs):
        employer = getattr(request.user, "employer", None)
        if employer is None:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "Account misconfigured. Please contact support.",
                },
                status=403,
            )

        uploaded_files = request.FILES.getlist("file")
        if not uploaded_files:
            return JsonResponse({"ok": False, "error": "No files received"}, status=400)

        salt_log_id = request.POST.get("salt_log") or request.POST.get("salt_log_id")
        if not salt_log_id:
            return JsonResponse({"ok": False, "error": "Missing salt log"}, status=400)

        salt_log = salt_logs_for_user(request.user).filter(pk=salt_log_id).first()
        if salt_log is None:
            return JsonResponse({"ok": False, "error": "Not found"}, status=404)
        if salt_log.status != SaltLog.STATUS_DRAFT:
            return JsonResponse(
                {"ok": False, "error": "Salt log is not a draft"}, status=409
            )

        if not salt_log.image_folder:
            salt_log.ensure_image_folder()
            salt_log.save(update_fields=["image_folder"])

        folder = _sanitize_folder(salt_log.image_folder) or "misc"
        # Same prefix get_s3_images_for_salt_log lists, so older photos stay visible.
        base_prefix = f"SALTLOG/{salt_log.user_employer}/{folder}"

        results = []
        for uploaded in uploaded_files:
            try:
                if getattr(uploaded, "size", 0) > MAX_SALT_IMAGE_BYTES:
                    results.append(
                        {
                            "name": uploaded.name,
                            "ok": False,
                            "error": "File too large (max 8MB).",
                        }
                    )
                    continue

                ctype = (uploaded.content_type or "").lower()
                if ctype not in ALLOWED_CONTENT_TYPES:
                    results.append(
                        {
                            "name": uploaded.name,
                            "ok": False,
                            "error": f"Unsupported content-type: {ctype}",
                        }
                    )
                    continue

                img = Image.open(uploaded)
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    pass
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img.thumbnail((1500, 1500), Image.LANCZOS)

                buf = BytesIO()
                img.save(
                    buf, format="JPEG", quality=85, optimize=True, progressive=True
                )
                buf.seek(0)
                key = f"{base_prefix}/{uuid4().hex}.jpg"
                upload_to_linode_object_storage(buf, key)
                results.append({"name": uploaded.name, "ok": True, "key": key})
                buf.close()
                img.close()
            except Exception as exc:
                results.append(
                    {
                        "name": getattr(uploaded, "name", "?"),
                        "ok": False,
                        "error": str(exc),
                    }
                )

        any_success = any(item.get("ok") for item in results)
        status_code = 200 if any_success else 400
        return JsonResponse(
            {"ok": any_success, "results": results}, status=status_code
        )


@login_required
def template_create(request):
    template = ChecklistTemplate(created_by=request.user)
    if request.method == "POST":
        form = ChecklistTemplateForm(request.POST, instance=template)
        formset = TemplateItemFormSet(request.POST, instance=template)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, "Checklist template created.")
            return redirect("template_detail", pk=template.pk)
    else:
        form = ChecklistTemplateForm(instance=template)
        formset = TemplateItemFormSet(instance=template)
    return render(
        request, "quiz/template_form.html", {"form": form, "formset": formset}
    )


@login_required
def template_edit(request, pk):
    template = get_object_or_404(ChecklistTemplate, pk=pk)
    if request.method == "POST":
        form = ChecklistTemplateForm(request.POST, instance=template)
        formset = TemplateItemFormSet(request.POST, instance=template)
        if form.is_valid() and formset.is_valid():
            form.save()
            formset.save()
            messages.success(request, "Template updated.")
            return redirect("template_detail", pk=template.pk)
    else:
        form = ChecklistTemplateForm(instance=template)
        formset = TemplateItemFormSet(instance=template)
    return render(
        request,
        "quiz/template_form.html",
        {"form": form, "formset": formset, "template": template},
    )


@login_required
def template_detail(request, pk):
    template = get_object_or_404(ChecklistTemplate, pk=pk)
    return render(request, "quiz/template_detail.html", {"template": template})


@login_required
def checklist_from_template(request, template_id):
    template = get_object_or_404(
        ChecklistTemplate.objects.prefetch_related("items"),
        pk=template_id,
        is_active=True,
    )

    if request.method == "POST":
        form = ChecklistForm(
            request.POST, user=request.user
        )  # pass user for store queryset
        if form.is_valid():
            checklist = form.save(commit=False)
            checklist.template = template
            checklist.created_by = request.user
            checklist.status = "draft"
            if not checklist.title:
                checklist.title = template.name
            checklist.save()

            # clone items in order
            t_items = template.items.all().order_by("order", "id")
            ChecklistItem.objects.bulk_create(
                [
                    ChecklistItem(
                        checklist=checklist,
                        template_item=ti,
                        text=ti.text,
                        section=ti.section or "",
                        result="",
                        order=(ti.order or idx),
                    )
                    for idx, ti in enumerate(t_items, start=1)
                ]
            )

            messages.success(request, "Checklist created.")
            return redirect("checklist_edit", slug=checklist.slug)
        else:
            messages.error(request, "Please fix the errors below.")
    else:
        form = ChecklistForm(initial={"title": template.name}, user=request.user)

    return render(
        request,
        "quiz/checklist_from_template.html",  # your template above
        {"template": template, "form": form},
    )


@login_required
def checklist_edit(request, slug):
    checklist = get_object_or_404(
        Checklist.objects.select_related("created_by", "submitted_by").prefetch_related(
            "items__template_item", "items__action_item"
        ),
        slug=slug,
    )

    if request.method == "POST":
        # Photos are uploaded separately; don't pass request.FILES here.
        # A native iOS multipart POST of this 80-item form can 400 before
        # this view (TooManyFieldsSent / MultiPartParserError). The edit
        # template posts the same fields via FormData like autosave.
        # ?autosave=1 is always a draft: never run submit validation, even
        # if a submit button named "action" is also in the body. Otherwise
        # a partial action plan rejects the whole POST and notes are dropped
        # while earlier answers stay saved.
        is_autosave = request.GET.get("autosave") == "1"
        action = "save" if is_autosave else (request.POST.get("action") or "save")
        form = ChecklistForm(request.POST, instance=checklist, user=request.user)
        formset = ChecklistItemFormSet(
            request.POST,
            instance=checklist,
            form_kwargs={"validate_submit": (not is_autosave) and action == "submit"},
        )

        if form.is_valid() and formset.is_valid():
            with transaction.atomic():
                # Backfill order for any rows that didn't post it
                next_pos = 0
                for fr in formset.forms:
                    cd = getattr(fr, "cleaned_data", {}) or {}
                    if cd.get("DELETE"):
                        continue
                    # if no order provided, assign sequentially
                    if not cd.get("order"):
                        fr.instance.order = next_pos
                    next_pos += 1

                form.save()
                formset.save()
                for fr in formset.forms:
                    if getattr(fr, "cleaned_data", None):
                        fr.save_action_item()

            if is_autosave:
                return JsonResponse(
                    {"ok": True, "saved_at": timezone.now().isoformat()}
                )

            if action == "submit":
                checklist.status = "submitted"
                checklist.submitted_by = request.user
                checklist.submitted_at = timezone.now()
                checklist.save(update_fields=["status", "submitted_by", "submitted_at"])
                # 🔔 Kick off async PDF generation
                try:
                    generate_checklist_pdf_task.delay(checklist.id)
                    if checklist_splits_action_plan(checklist):
                        messages.success(
                            request,
                            "Checklist submitted. The checklist PDF and a separate "
                            "action-plan PDF are being generated.",
                        )
                    else:
                        messages.success(
                            request, "Checklist submitted. PDF generation started."
                        )
                except Exception as e:
                    messages.error(
                        request,
                        f"Checklist submitted, but PDF task failed to queue: {e}",
                    )

                return redirect("checklist_dashboard")
            else:
                messages.success(request, "Draft saved.")
                return redirect("checklist_edit", slug=checklist.slug)

        if is_autosave:
            return JsonResponse({"ok": False}, status=422)

        # INVALID: dump errors to terminal and fall through to re-render bound forms
        print("Form errors:", form.errors.as_json())
        print("Form non-field errors:", form.non_field_errors())
        print("Formset non-form errors:", formset.non_form_errors())
        for i, fr in enumerate(formset.forms):
            if fr.errors:
                print(f"Item {i} field errors:", fr.errors.as_json())
            nfe = fr.non_field_errors()
            if nfe:
                print(f"Item {i} non-field errors:", nfe)

    else:
        form = ChecklistForm(instance=checklist, user=request.user)
        formset = ChecklistItemFormSet(instance=checklist)

    # Annotate thumbnails (works for GET and invalid POST re-render)
    employer = getattr(request.user, "employer", "noemployer")
    for fr in formset.forms:
        item = fr.instance
        item.signed_url = None  # default
        if (
            getattr(item, "pk", None)
            and getattr(item, "photo", None)
            and item.photo.name
        ):
            # short-lived is fine for the edit page
            item.signed_url = get_signed_url_for_key(item.photo.name, expires_in=900)

    return render(
        request,
        "quiz/checklist_edit.html",
        {
            "checklist": checklist,
            "form": form,
            "formset": formset,
            "item_error_summary": formset.item_error_summaries(),
        },
    )


@login_required
def checklist_detail(request, slug):
    checklist = get_object_or_404(
        Checklist.objects.select_related("created_by", "submitted_by", "store"),
        slug=slug,
    )

    qs = (
        ChecklistItem.objects.filter(checklist=checklist)
        .select_related("template_item", "action_item")
        .order_by("order", "id")
    )

    items = []
    for it in qs:
        signed = None
        if it.photo and it.photo.name:
            # fast: sign key, no listing
            signed = get_signed_url_for_key(it.photo.name, expires_in=900)
        action = it.get_action_item()
        items.append(
            {
                "text": it.text,
                "section": it.section,
                "result": it.result,
                "answer": it.answer,
                "responsibility": it.responsibility,
                "text_value": it.text_value,
                "comment": it.comment,
                "signed_url": signed,
                "action": action,
            }
        )

    pdf_url = None
    if getattr(checklist, "pdf_key", ""):
        # If you have a function to turn your Dropbox path into a shareable URL, use it here.
        # pdf_url = dropbox_shared_link_for_path(checklist.pdf_key)
        pdf_url = None  # or keep None and the button will show "not ready"

    return render(
        request,
        "quiz/checklist_detail.html",
        {
            "checklist": checklist,
            "items": items,
            "action_items": checklist.action_items.select_related(
                "checklist_item"
            ).order_by("checklist_item__order", "id"),
            "pdf_url": pdf_url,
        },
    )


@login_required
def checklist_list(request):
    """
    Lists available checklists with quick filters and a link to resume/complete.
    """
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()  # draft|submitted|completed
    mine = request.GET.get("mine")

    qs = Checklist.objects.select_related("created_by", "submitted_by").order_by(
        "-created_at"
    )

    # Common quick filters (tweak to your multi-tenant/employer scoping as needed)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(notes__icontains=q))
    if status in {"draft", "submitted", "completed"}:
        qs = qs.filter(status=status)
    if mine == "1":
        qs = qs.filter(created_by=request.user)

    paginator = Paginator(qs, 20)
    page = request.GET.get("page")
    page_obj = paginator.get_page(page)

    return render(
        request,
        "quiz/checklist_list.html",
        {
            "page_obj": page_obj,
            "q": q,
            "status": status,
            "mine": mine,
        },
    )


@login_required
def checklist_edit_by_id(request, pk: int):
    """
    Compatibility shim: if someone hits /checklists/<pk>/edit/,
    redirect to the canonical slug URL.
    """
    checklist = get_object_or_404(Checklist, pk=pk)
    return redirect("checklist_edit", slug=checklist.slug)


@login_required
def checklist_dashboard(request):
    """
    Dashboard with: available templates, in-progress (draft), and submitted.
    """
    q = (request.GET.get("q") or "").strip()
    mine = request.GET.get("mine") == "1"

    templates = ChecklistTemplate.objects.filter(is_active=True)
    if q:
        templates = templates.filter(Q(name__icontains=q) | Q(description__icontains=q))
    templates = templates.order_by("name").prefetch_related("items")

    drafts = Checklist.objects.filter(status="draft")
    submitted = Checklist.objects.filter(status="submitted")
    # If you want to show completed too, uncomment:
    # completed = Checklist.objects.filter(status="completed")

    if q:
        drafts = drafts.filter(Q(title__icontains=q) | Q(notes__icontains=q))
        submitted = submitted.filter(Q(title__icontains=q) | Q(notes__icontains=q))
        # completed = completed.filter(Q(title__icontains=q) | Q(notes__icontains=q))

    if mine:
        drafts = drafts.filter(created_by=request.user)
        submitted = submitted.filter(
            Q(created_by=request.user) | Q(submitted_by=request.user)
        )
        # completed = completed.filter(Q(created_by=request.user) | Q(submitted_by=request.user))

    drafts = drafts.select_related("created_by").order_by("-created_at")
    submitted = submitted.select_related("submitted_by").order_by(
        "-submitted_at", "-created_at"
    )
    # completed = completed.order_by("-submitted_at", "-created_at")

    ctx = {
        "q": q,
        "mine": "1" if mine else "",
        "templates": templates,
        "drafts": drafts,
        "submitted": submitted,
        # "completed": completed,
    }
    return render(request, "quiz/checklist_dashboard.html", ctx)


# ------------------------------------------------ #

# Upload images for checklists


ALLOWED_CT = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/tiff",
    "image/webp",
    "application/octet-stream",
    None,
}
MAX_BYTES = 50 * 1024 * 1024  # 10 MB


class UploadChecklistItemPhotoView(LoginRequiredMixin, View):
    login_url = "/login/"

    def post(self, request, slug, item_uuid):
        file = request.FILES.get("file")
        if not file:
            return JsonResponse({"error": "No file provided"}, status=400)
        # Now safe to log size
        logger.info(
            "📸 Incoming upload: name=%s size=%.2f MB ct=%s",
            getattr(file, "name", ""),
            getattr(file, "size", 0) / 1024 / 1024,
            getattr(file, "content_type", None),
        )

        checklist = get_object_or_404(Checklist, slug=slug)

        # 🔒 Multi-tenant guard
        employer = getattr(request.user, "employer", None)
        # print("Employer :", employer)
        if employer is None:
            # This should never happen for a logged-in user
            logger.error(
                "NO_EMPLOYER: user_id=%s path=%s checklist_slug=%s",
                getattr(request.user, "id", None),
                request.path,
                slug,
            )
            return JsonResponse(
                {"error": "Account misconfigured. Please contact support."}, status=403
            )
        # 🔒 Extra guard: make sure checklist belongs to same employer
        checklist_employer = getattr(
            getattr(checklist, "created_by", None), "employer", None
        )
        if checklist_employer and checklist_employer != employer:
            logger.warning(
                "TENANT_MISMATCH: user_emp=%s checklist_emp=%s slug=%s",
                getattr(employer, "id", None),
                getattr(checklist_employer, "id", None),
                slug,
            )
            return JsonResponse({"error": "Forbidden"}, status=403)

        item = get_object_or_404(ChecklistItem, checklist=checklist, uuid=item_uuid)

        # Size guard
        if getattr(file, "size", 0) > MAX_BYTES:
            return JsonResponse({"error": "File too large (max 10MB)."}, status=400)

        # Content-type (defensive)
        ct = getattr(file, "content_type", None)
        if ct not in ALLOWED_CT:
            return JsonResponse({"error": f"Unsupported type: {ct}"}, status=400)
        print("ct :", ct)
        try:
            # ✅ Normalize all images to JPEG, consistent with store uploads
            # For checklist images we can be a tad smaller to keep UI snappy:
            # long edge 1800, ~800–900KB target
            buf, ext = normalize_to_jpeg(
                file,
                target_long_edge=1800,
                target_bytes=850_000,
            )
            print("buffer :", buf)
            employer_segment = slugify(str(getattr(employer, "name", "noemployer")))
            base, _ = os.path.splitext(file.name or "")
            filename = f"{slugify(base) or 'photo'}-{uuid.uuid4().hex[:8]}.jpg"
            key = (
                f"CHECKLISTS/{employer_segment}/{checklist.slug}/{item.uuid}/{filename}"
            )

            upload_to_linode_object_storage(buf, key)
            if isinstance(buf, BytesIO):
                buf.close()

            # Persist and respond
            item.photo.name = key
            item.save(update_fields=["photo"])

            signed_url = get_signed_url_for_key(key, expires_in=3600)
            return JsonResponse({"message": "ok", "key": key, "url": signed_url})

        except UnidentifiedImageError:
            return JsonResponse({"error": "Unreadable image file."}, status=400)
        except Image.DecompressionBombError:
            return JsonResponse(
                {"error": "Image too large (pixel dimensions)."}, status=400
            )
        except Exception as e:
            return JsonResponse({"error": f"Upload failed: {e}"}, status=500)


# ------------------------------------------------ #


@login_required
def checklist_download_fresh_start_api(request, slug):
    checklist = get_object_or_404(Checklist, slug=slug)
    async_res = generate_fresh_checklist_pdf.delay(checklist.id)
    return JsonResponse({"task_id": async_res.id})


@login_required
def checklist_download_fresh_status(request):
    task_id = request.GET.get("task_id")
    if not task_id:
        return HttpResponseBadRequest("Missing task_id")

    res = AsyncResult(task_id)
    if not res.ready():
        return JsonResponse({"ready": False}, status=202)

    if res.failed():
        # show the actual exception message from the task
        return JsonResponse(
            {"ready": True, "error": str(res.info) or "PDF generation failed"},
            status=200,
        )

    key = res.result  # object key returned by the task
    url = get_signed_url_for_key(key, expires_in=120)
    return JsonResponse({"ready": True, "url": url}, status=200)


@login_required
def checklist_report_html(request, slug):
    """
    Render the checklist report HTML (same context as the PDF) directly in the browser.
    Useful for debugging layout/styles without generating a PDF.
    """
    checklist = (
        Checklist.objects.select_related("created_by", "submitted_by", "store")
        .prefetch_related("items")
        .get(slug=slug)
    )
    print("checklist store :", checklist.store)
    # Build items with short-lived signed URLs (so images load in browser)
    items = []
    for it in checklist.items.select_related("action_item").order_by("order", "id"):
        photo_url = None
        if it.photo and it.photo.name:
            photo_url = get_signed_url_for_key(it.photo.name, expires_in=900)
        action = it.get_action_item()
        items.append(
            {
                "text": it.text,
                "result": it.result,
                "answer": it.answer,
                "responsibility": it.responsibility,
                "text_value": it.text_value,
                "comment": it.comment,
                "photo_url": photo_url,
                "action": action,
            }
        )

    ctx = {
        "checklist": checklist,
        "items": items,
        "action_items": checklist.action_items.select_related("checklist_item"),
        **store_report_context(checklist.store),
    }
    # Reuse the exact same template as the PDF:
    return render(request, "quiz/checklist_pdf_for_app.html", ctx)
