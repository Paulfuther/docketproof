from django import forms
from django.contrib import admin, messages
from django.db.models import Q
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from arl.user.models import CustomUser

from .admin_repair import repair_flow_step
from .forms import BulkDynamicRepairRowForm, OnboardingRepairFilterForm
from .models import (DocumentFlow, DocumentFlowStep, ImmigrationStatusEvent,
                     SentDocuSignEnvelope)


class ImmigrationStatusEventAdminForm(forms.ModelForm):
    class Meta:
        model = ImmigrationStatusEvent
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("created_by"):
            user = getattr(self.__class__, "request_user", None)
            if getattr(user, "pk", None):
                cleaned["created_by"] = user
        return cleaned


class DocumentFlowStepInline(admin.TabularInline):
    model = DocumentFlowStep
    extra = 1


@admin.register(DocumentFlowStep)
class DocumentFlowStepAdmin(admin.ModelAdmin):
    list_display = (
        "flow",
        "step_order",
        "template",
        "label",
        "is_required",
        "is_active",
    )
    list_filter = ("flow__employer", "is_required", "is_active")
    search_fields = ("flow__name", "template__template_name", "label")
    ordering = ("flow", "step_order")


@admin.register(SentDocuSignEnvelope)
class SentDocuSignEnvelopeAdmin(admin.ModelAdmin):
    list_display = (
        "template_name",
        "user",
        "employer",
        "status",
        "sent_at",
        "completed_at",
    )
    list_filter = ("status", "employer")
    search_fields = (
        "template_name",
        "envelope_id",
        "user__first_name",
        "user__last_name",
        "user__email",
    )


@admin.register(DocumentFlow)
class DocumentFlowAdmin(admin.ModelAdmin):
    list_display = ("name", "employer", "is_default", "is_active")
    inlines = [DocumentFlowStepInline]
    change_list_template = "admin/documentflow/documentflow_change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "onboarding-repair-dashboard/",
                self.admin_site.admin_view(self.onboarding_repair_dashboard_view),
                name="documentflow_onboarding_repair_dashboard",
            ),
        ]
        return custom_urls + urls

    def onboarding_repair_dashboard_view(self, request):
        data = request.POST if request.method == "POST" else request.GET
        filter_form = OnboardingRepairFilterForm(data or None)

        selected_employer = None
        selected_flow = None
        search = ""
        incomplete_only = False

        if filter_form.is_valid():
            selected_employer = filter_form.cleaned_data.get("employer")
            selected_flow = filter_form.cleaned_data.get("flow")
            search = (filter_form.cleaned_data.get("q") or "").strip()
            incomplete_only = bool(filter_form.cleaned_data.get("incomplete_only"))

        if not selected_flow and selected_employer:
            selected_flow = (
                DocumentFlow.objects.filter(
                    employer=selected_employer,
                    is_default=True,
                    is_active=True,
                ).first()
                or DocumentFlow.objects.filter(
                    employer=selected_employer,
                    is_active=True,
                ).first()
            )

        steps = []
        users = CustomUser.objects.none()
        rows = []

        if selected_flow:
            steps = list(
                selected_flow.steps.select_related("template")
                .filter(is_active=True)
                .order_by("step_order", "id")
            )

            users = (
                CustomUser.objects.filter(
                    is_active=True,
                    employer=selected_flow.employer,
                )
                .select_related("employer", "store")
                .order_by("email")
            )

            if search:
                users = users.filter(
                    Q(first_name__icontains=search)
                    | Q(last_name__icontains=search)
                    | Q(email__icontains=search)
                )

            users = users[:200]

        if request.method == "POST" and selected_flow:
            updated = 0

            for user in users:
                form = BulkDynamicRepairRowForm(
                    request.POST,
                    prefix=f"user_{user.id}",
                    steps=steps,
                )

                if not form.is_valid():
                    continue

                if not form.cleaned_data.get("apply"):
                    continue

                changed_any_step = False

                for step in steps:
                    employee_status = form.cleaned_data.get(
                        f"step_{step.id}_employee_status"
                    ) or ""
                    manager_status = form.cleaned_data.get(
                        f"step_{step.id}_manager_status"
                    ) or ""
                    manager_name = form.cleaned_data.get(
                        f"step_{step.id}_manager_name"
                    ) or ""
                    manager_email = form.cleaned_data.get(
                        f"step_{step.id}_manager_email"
                    ) or ""

                    if not employee_status and not manager_status:
                        continue

                    repair_flow_step(
                        user=user,
                        flow=selected_flow,
                        step=step,
                        employee_status=employee_status,
                        manager_status=manager_status,
                        manager_name=manager_name,
                        manager_email=manager_email,
                    )
                    changed_any_step = True

                if changed_any_step:
                    updated += 1

            messages.success(request, f"Updated {updated} employee row(s).")

            url = reverse("admin:documentflow_onboarding_repair_dashboard")
            params = []
            if selected_employer:
                params.append(f"employer={selected_employer.id}")
            if selected_flow:
                params.append(f"flow={selected_flow.id}")
            if search:
                params.append(f"q={search}")
            if incomplete_only:
                params.append("incomplete_only=on")

            return redirect(f"{url}?{'&'.join(params)}" if params else url)

        if selected_flow:
            for user in users:
                step_rows = []
                has_incomplete = False

                initial_data = {
                    "user_id": user.id,
                }

                for step in steps:
                    env = (
                        SentDocuSignEnvelope.objects.filter(
                            user=user,
                            flow=selected_flow,
                            flow_step=step,
                        )
                        .select_related("template", "flow_step")
                        .prefetch_related("recipients")
                        .first()
                    )

                    recipients = (
                        list(env.recipients.order_by("routing_order", "id"))
                        if env
                        else []
                    )

                    current_status = env.status if env else "not sent"
                    if current_status != "completed":
                        has_incomplete = True

                    gsa_rec = next(
                        (r for r in recipients if r.routing_order == 1),
                        None,
                    )
                    mgr_rec = next(
                        (r for r in recipients if r.routing_order == 2),
                        None,
                    )

                    initial_data[f"step_{step.id}_employee_status"] = ""
                    initial_data[f"step_{step.id}_manager_status"] = ""
                    initial_data[f"step_{step.id}_manager_name"] = (
                        mgr_rec.name if mgr_rec else ""
                    )
                    initial_data[f"step_{step.id}_manager_email"] = (
                        mgr_rec.email if mgr_rec else ""
                    )

                    step_rows.append(
                        {
                            "step": step,
                            "env": env,
                            "current_status": current_status,
                            "gsa_rec": gsa_rec,
                            "mgr_rec": mgr_rec,
                        }
                    )

                if incomplete_only and not has_incomplete:
                    continue

                form = BulkDynamicRepairRowForm(
                    prefix=f"user_{user.id}",
                    steps=steps,
                    initial=initial_data,
                )

                rendered_step_rows = []
                for step_row in step_rows:
                    step = step_row["step"]
                    rendered_step_rows.append(
                        {
                            "step": step,
                            "env": step_row["env"],
                            "current_status": step_row["current_status"],
                            "gsa_rec": step_row["gsa_rec"],
                            "mgr_rec": step_row["mgr_rec"],
                            "employee_status_field": form[
                                f"step_{step.id}_employee_status"
                            ],
                            "manager_status_field": form[
                                f"step_{step.id}_manager_status"
                            ],
                            "manager_name_field": form[
                                f"step_{step.id}_manager_name"
                            ],
                            "manager_email_field": form[
                                f"step_{step.id}_manager_email"
                            ],
                        }
                    )

                rows.append(
                    {
                        "user": user,
                        "form": form,
                        "step_rows": rendered_step_rows,
                    }
                )

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Onboarding Repair Dashboard",
            "filter_form": filter_form,
            "flow": selected_flow,
            "steps": steps,
            "rows": rows,
            "search": search,
            "incomplete_only": incomplete_only,
        }

        return TemplateResponse(
            request,
            "admin/documentflow/onboarding_repair_dashboard.html",
            context,
        )


@admin.register(ImmigrationStatusEvent)
class ImmigrationStatusEventAdmin(admin.ModelAdmin):
    form = ImmigrationStatusEventAdminForm
    list_display = (
        "user",
        "employer",
        "status_type",
        "reference_number",
        "effective_date",
        "expiry_date",
        "is_active",
        "created_by",
        "created_at",
    )

    list_filter = (
        "status_type",
        "is_active",
        "employer",
        "effective_date",
        "expiry_date",
        "created_at",
    )

    search_fields = (
        "user__first_name",
        "user__last_name",
        "user__email",
        "reference_number",
        "notes",
        "document_file__file_name",
        "document_file__document_title",
    )

    readonly_fields = (
        "created_at",
    )

    autocomplete_fields = (
        "user",
        "employer",
        "document_file",
        "created_by",
    )

    ordering = ("-created_at",)
    list_per_page = 25

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.request_user = request.user
        return form
