from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from .checklist_import import ChecklistImportError, ChecklistTemplateImportForm
from .models import (
    Checklist,
    ChecklistActionItem,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateItem,
)


class ChecklistTemplateItemInline(admin.TabularInline):
    model = ChecklistTemplateItem
    extra = 0
    fields = (
        "order",
        "item_code",
        "section",
        "text",
        "response_type",
        "required",
        "responsibility_assignable",
        "create_action_on",
        "requires_photo",
        "allow_photo",
    )


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
