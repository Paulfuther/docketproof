from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from .checklist_import import ChecklistImportError, ChecklistTemplateImportForm
from .forms import ChecklistTemplateItemAdminForm, split_create_action_on
from .models import (
    Checklist,
    ChecklistActionItem,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateItem,
)


class ChecklistTemplateItemInline(admin.TabularInline):
    model = ChecklistTemplateItem
    form = ChecklistTemplateItemAdminForm
    extra = 0
    show_change_link = True
    fields = (
        "order",
        "item_code",
        "section",
        "text",
        "response_type",
        "required",
        "follow_up_on_yes",
        "follow_up_on_no",
        "responsibility_assignable",
        "requires_photo",
        "allow_photo",
    )


@admin.register(ChecklistTemplateItem)
class ChecklistTemplateItemAdmin(admin.ModelAdmin):
    form = ChecklistTemplateItemAdminForm
    list_display = (
        "text",
        "template",
        "response_type",
        "follow_up_when",
        "responsibility_assignable",
        "order",
    )
    list_filter = ("response_type", "responsibility_assignable")
    search_fields = ("text", "item_code", "section", "template__name")

    def get_fieldsets(self, request, obj=None):
        follow_up_fields = [
            "follow_up_on_yes",
            "follow_up_on_no",
            "responsibility_assignable",
        ]
        stored = ["N"] if obj is None else obj.create_action_on
        _, _, extra = split_create_action_on(stored)
        if extra is None or extra:
            follow_up_fields.append("create_action_on_raw")
        return (
            (
                None,
                {
                    "fields": (
                        "template",
                        "order",
                        "item_code",
                        "section",
                        "text",
                        "response_type",
                        "required",
                    )
                },
            ),
            (
                "Follow-up",
                {
                    "description": (
                        "Check the answer that needs a follow-up. "
                        "Require L or S applies only to that answer. "
                        "Yes does not require L unless Follow-up when Yes "
                        "and Require L or S are both checked."
                    ),
                    "fields": follow_up_fields,
                },
            ),
            (
                "Photos",
                {
                    "fields": (
                        "requires_photo",
                        "allow_photo",
                        "action_plan_form",
                    )
                },
            ),
        )

    @admin.display(description="Follow-up when")
    def follow_up_when(self, obj):
        yes, no, extra = split_create_action_on(obj.create_action_on)
        parts = []
        if yes:
            parts.append("Yes")
        if no:
            parts.append("No")
        if extra:
            parts.extend(str(code) for code in extra)
        if extra is None:
            return "Advanced"
        return ", ".join(parts) if parts else "—"


@admin.register(ChecklistTemplate)
class ChecklistTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "document_id",
        "is_active",
        "created_by",
        "created_at",
    )
    search_fields = ("name", "document_id", "parent_sop")
    inlines = [ChecklistTemplateItemInline]
    change_list_template = "admin/quiz/checklisttemplate/change_list.html"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-json/",
                self.admin_site.admin_view(self.import_json_view),
                name="quiz_checklisttemplate_import_json",
            ),
        ]
        return custom_urls + urls

    def import_json_view(self, request):
        if not self.has_add_permission(request):
            raise PermissionDenied

        if request.method == "POST":
            form = ChecklistTemplateImportForm(request.POST, request.FILES)
            if form.is_valid():
                try:
                    template = form.save(created_by=request.user)
                except ChecklistImportError as exc:
                    form.add_error(None, str(exc))
                else:
                    item_count = template.items.count()
                    self.message_user(
                        request,
                        f'Imported "{template.name}" with {item_count} item(s). '
                        "Photos stay optional unless requires_photo is set.",
                        messages.SUCCESS,
                    )
                    return redirect(
                        reverse(
                            "admin:quiz_checklisttemplate_change",
                            args=[template.pk],
                        )
                    )
        else:
            form = ChecklistTemplateImportForm()

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "form": form,
            "title": "Import checklist template from JSON",
        }
        return TemplateResponse(
            request,
            "admin/quiz/checklisttemplate/import_json.html",
            context,
        )


class ChecklistActionItemInline(admin.StackedInline):
    model = ChecklistActionItem
    extra = 0
    fields = (
        "action_item",
        "action_required",
        "who",
        "target_date",
        "completion_date",
        "status",
        "note",
    )


class ChecklistItemInline(admin.TabularInline):
    model = ChecklistItem
    extra = 0
    fields = (
        "order",
        "text",
        "result",
        "responsibility",
        "text_value",
        "comment",
    )
    show_change_link = True


@admin.register(Checklist)
class ChecklistAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "status",
        "created_by",
        "submitted_by",
        "created_at",
        "submitted_at",
    )
    inlines = [ChecklistItemInline]


@admin.register(ChecklistItem)
class ChecklistItemAdmin(admin.ModelAdmin):
    list_display = ("text", "checklist", "result", "responsibility", "order")
    list_filter = ("result", "responsibility")
    search_fields = ("text", "checklist__title")
    inlines = [ChecklistActionItemInline]


@admin.register(ChecklistActionItem)
class ChecklistActionItemAdmin(admin.ModelAdmin):
    list_display = (
        "action_item",
        "checklist",
        "who",
        "target_date",
        "status",
    )
    list_filter = ("status",)
    search_fields = ("action_item", "who", "action_required")
