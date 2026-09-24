import logging
import os
import uuid
from io import BytesIO
from uuid import uuid4

from arl.helpers import (
    get_s3_images_for_salt_log,
    get_signed_url_for_key,
    upload_to_linode_object_storage,
)
from arl.utils.images import normalize_to_jpeg
from celery.result import AsyncResult
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.db.models import Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.text import slugify
from django.views import View
from django.views.generic import CreateView, UpdateView, View
from django.views.generic.list import ListView
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
from .models import Checklist, ChecklistItem, ChecklistTemplate, Quiz, SaltLog
from .store_address import store_report_context
from .tasks import (
    generate_checklist_pdf_task,
    generate_fresh_checklist_pdf,
    generate_salt_log_pdf_task,
    save_salt_log,
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
    questions = list(quiz.questions.prefetch_related("answers"))

    if request.method == "POST":
        has_errors = False
        for question in questions:
            selected = (request.POST.get(f"question_{question.id}") or "").strip()
            follow_up = request.POST.get(f"follow_up_{question.id}") or ""
            responsibility = request.POST.get(f"responsibility_{question.id}") or ""
            question.posted_answer = selected.lower()
            question.posted_follow_up = follow_up.strip()
            question.posted_responsibility = responsibility.strip().upper()
            question.form_errors = question.submission_errors(
                question.posted_answer,
                question.posted_follow_up,
                question.posted_responsibility,
            )
            if question.form_errors:
                has_errors = True

        if has_errors:
            return render(
                request,
                "quiz/take_quiz.html",
                {
                    "quiz": quiz,
                    "questions": questions,
                    "form_error": (
                        "Complete the follow-up items marked on this quiz."
                    ),
                },
            )

        score = sum(
            1 for question in questions if question.selected_is_correct(
                question.posted_answer
            )
        )
        return render(
            request,
            "quiz/quiz_result.html",
            {
                "quiz": quiz,
                "score": score,
                "total_questions": len(questions),
            },
        )

    return render(
        request, "quiz/take_quiz.html", {"quiz": quiz, "questions": questions}
    )


class SaltLogCreateView(LoginRequiredMixin, CreateView):
    model = SaltLog
    form_class = SaltLogForm
    template_name = "quiz/salt_log_form.html"
    success_url = reverse_lazy("home")

    def dispatch(self, request, *args, **kwargs):
        print("Dispatch method called.")
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        # Pass the user to the form to initialize certain fields
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get(self, request, *args, **kwargs):
        # Initialize the form with the user instance passed to the form
        form = self.form_class(user=self.request.user)
        return self.render_to_response({"form": form})

    def form_valid(self, form):
        # Set the user and user_employer fields for the form instance
        form.instance.user = self.request.user
        form.instance.user_employer = self.request.user.employer
        print(form.instance.user, form.instance.user_employer)
        # Serialize form data to pass to the Celery task
        form_data = self.serialize_form_data(form.cleaned_data)

        # Trigger the Celery task to save form data
        save_salt_log.delay(**form_data)

        # Show a success message
        messages.success(self.request, "Salt Log Added.")

        return redirect("home")

    def form_invalid(self, form):
        # Render the form again with validation errors
        print(form.errors)
        return self.render_to_response({"form": form})

    def serialize_form_data(self, form_data):
        # Convert ForeignKey fields to their primary key values
        form_data["store"] = (
            form_data["store"].pk
            if "store" in form_data and form_data["store"] is not None
            else None
        )
        form_data["user_employer"] = (
            form_data["user_employer"].pk
            if "user_employer" in form_data and form_data["user_employer"] is not None
            else None
        )
        form_data["user"] = self.request.user.pk  # Add the user's ID for task
        return form_data


# Accept only basic image mimes that browsers/cameras send commonly
ALLOWED_CONTENT_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/heic",
    "image/heif",
    "image/webp",
}


def _sanitize_folder(s: str) -> str:
    # keep it simple: strip spaces, remove path traversal, collapse weird chars
    s = (s or "").strip().strip("/").replace("..", "")
    # optional: further restrict to safe chars
    return "".join(ch for ch in s if ch.isalnum() or ch in ("-", "_", "/"))


class ProcessSaltLogImagesView(LoginRequiredMixin, View):
    login_url = "/login/"

    def post(self, request, *args, **kwargs):
        user = request.user
        employer = user.employer

        # Dropzone sends 'file' (one per request by default; can be many if uploadMultiple=true)
        uploaded_files = request.FILES.getlist("file")
        if not uploaded_files:
            return JsonResponse({"ok": False, "error": "No files received"}, status=400)

        raw_folder = request.POST.get("image_folder", "")
        image_folder = _sanitize_folder(raw_folder) or "misc"
        base_prefix = f"SALTLOG/{employer}/{image_folder}"

        results = []
        for uploaded in uploaded_files:
            try:
                # Basic content-type guard (client-supplied but useful)
                ctype = (uploaded.content_type or "").lower()
                if ctype and ctype not in ALLOWED_CONTENT_TYPES:
                    results.append(
                        {
                            "name": uploaded.name,
                            "ok": False,
                            "error": f"Unsupported content-type: {ctype}",
                        }
                    )
                    continue

                # Open and normalize
                img = Image.open(uploaded)

                # Autorotate by EXIF orientation (no-op if none)
                try:
                    img = ImageOps.exif_transpose(img)
                except Exception:
                    # ignore EXIF issues — keep going
                    pass

                # Convert to RGB so we can save as JPEG
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")

                # Resize in-place keeping aspect ratio (max 1500px)
                img.thumbnail((1500, 1500), Image.LANCZOS)

                # Encode as JPEG (progressive helps size; tune quality as you like)
                buf = BytesIO()
                img.save(
                    buf, format="JPEG", quality=85, optimize=True, progressive=True
                )
                buf.seek(0)

                # Always use a unique filename and .jpg extension (since we encoded JPEG)
                unique_name = f"{uuid4().hex}.jpg"
                key = f"{base_prefix}/{unique_name}"

                # Your helper likely takes a file-like and a key
                upload_to_linode_object_storage(buf, key)

                results.append({"name": uploaded.name, "ok": True, "key": key})
                buf.close()
                img.close()
            except Exception as e:
                results.append(
                    {
                        "name": getattr(uploaded, "name", "?"),
                        "ok": False,
                        "error": str(e),
                    }
                )

        # Consider overall success only if at least one succeeded
        any_success = any(r.get("ok") for r in results)
        status_code = 200 if any_success else 400
        return JsonResponse({"ok": any_success, "results": results}, status=status_code)


class SaltLogListView(LoginRequiredMixin, ListView):
    model = SaltLog
    template_name = "quiz/salt_log_list.html"
    context_object_name = "saltlogs"

    def get_queryset(self):
        # Filter salt logs by the current user's employer
        return SaltLog.objects.filter(user_employer=self.request.user.employer)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["defer_render"] = True
        return context


class SaltLogUpdateView(LoginRequiredMixin, UpdateView):
    model = SaltLog
    login_url = "/login/"
    form_class = SaltLogForm
    template_name = "quiz/salt_log_form_update.html"
    success_url = reverse_lazy("salt_log_list")

    def dispatch(self, request, *args, **kwargs):
        try:
            # Your print statement for debugging
            print("Dispatch method called.")
            return super().dispatch(request, *args, **kwargs)
        except Exception as e:
            print(f"Exception occurred: {e}")
            raise

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        user = self.request.user
        employer = user.employer
        existing_images = []

        # Check if image_folder exists and get available images from S3
        if self.object.image_folder:
            existing_images = get_s3_images_for_salt_log(
                self.object.image_folder, user.employer
            )
        print(existing_images)
        form = self.form_class(
            instance=self.object, initial={"existing_images": existing_images}
        )
        form.fields["user_employer"].initial = employer

        return self.render_to_response(
            self.get_context_data(
                form=form, existing_images=existing_images, user_employer=employer
            )
        )

    def form_valid(self, form):
        # print(form)
        form.instance.user_employer = self.request.user.employer
        return super().form_valid(form)

    def form_invalid(self, form):
        return self.render_to_response(self.get_context_data(form=form))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["user"] = self.request.user
        return context


# this route is turned off in the urls.


def generate_salt_log_pdf(request):
    # Fetch the SaltLog with incident_id = 1
    incident = get_object_or_404(SaltLog, pk=23)
    images = get_s3_images_for_salt_log(incident.image_folder, incident.user_employer)

    # Attempt to trigger the PDF generation task
    try:
        generate_salt_log_pdf_task.delay(incident.id)
        messages.success(request, "PDF generation task initiated successfully.")
    except Exception as e:
        messages.error(request, f"An error occurred while generating the PDF: {e}")

    context = {
        "incident": incident,
        "images": images,
    }
    return render(request, "quiz/salt_log_form_pdf.html", context)


@receiver(post_save, sender=SaltLog)
def handle_new_incident_form_creation(sender, instance, created, **kwargs):
    if created:
        try:
            generate_salt_log_pdf_task.delay(instance.id)
        except Exception as e:
            print(f"An error occurred: {e}")


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
        action = request.POST.get("action")
        form = ChecklistForm(request.POST, instance=checklist, user=request.user)
        formset = ChecklistItemFormSet(
            request.POST,
            instance=checklist,
            form_kwargs={"validate_submit": action == "submit"},
        )

        if form.is_valid() and formset.is_valid():
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

            if action == "submit":
                checklist.status = "submitted"
                checklist.submitted_by = request.user
                checklist.submitted_at = timezone.now()
                checklist.save(update_fields=["status", "submitted_by", "submitted_at"])
                # 🔔 Kick off async PDF generation
                try:
                    generate_checklist_pdf_task.delay(checklist.id)
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
