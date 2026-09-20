from django.contrib import admin

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
