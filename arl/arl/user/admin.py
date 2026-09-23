import uuid
from django import forms
from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter
from django.contrib.auth.admin import UserAdmin
from django.forms.widgets import TextInput
from django.shortcuts import redirect, render
from django.urls import path
from django.utils.html import format_html, strip_tags
from import_export import fields, resources
from import_export import fields as export_fields
from import_export.admin import ExportActionMixin
from django_celery_results.admin import (
    TaskResultAdmin as DefaultTaskResultAdmin)
from arl.bucket.helpers import upload_to_linode_object_storage
from arl.carwash.models import CarwashStatus
from arl.dsign.models import (
    DocuSignTemplate,
    ProcessedDocsignDocument,
    SignedDocumentFile,
)
from arl.incident.models import Incident, MajorIncident
from arl.msg.models import (
    BulkEmailSendgrid,
    EmailTemplate,
    Twimlmessages,
    UserConsent,
    WhatsAppTemplate,
)
# from arl.msg.tasks import EmployerSMSTask
# from arl.payroll.models import CalendarEvent, PayPeriod, StatutoryHoliday
from arl.quiz.models import Answer, Question, Quiz, SaltLog
from arl.setup.models import StripePayment, StripePlan, TenantApiKeys

from .models import (
    CustomUser,
    DocumentType,
    EmployeeDocument,
    Employer,
    EmployerSettings,
    ErrorLog,
    ExternalRecipient,
    NewHireInvite,
    SMSOptOut,
    Store,
    UserManager,
)
from arl.utils.crypto import normalize_digits, sin_hash


class ExternalRecipientAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "company", "email", "group")
    search_fields = ("first_name", "last_name", "company", "email",
                     "group__name")


# This class is used for exporting
class UserResource(resources.ModelResource):
    manager = export_fields.Field()
    whatsapp_consent = export_fields.Field()
    store = fields.Field(column_name="store", readonly=True)
    all_docusign_templates = fields.Field(
        column_name="Docusign Documents", attribute="all_docusign_templates"
    )
    work_permit_expiration_date = fields.Field(
        column_name="Permit Expiration",
        attribute=("work_permit_expiration_date")
    )
    sin_expiration_date = fields.Field(
        column_name="SIN Expiration", attribute="sin_expiration_date"
    )

    class Meta:
        model = CustomUser
        exclude = ("sin", "sin_encrypted", "sin_hash")
        fields = (
            "username",
            "first_name",
            "last_name",
            "email",
            "store",
            "phone_number",
            "manager",
            "whatsapp_consent",
            "sin_last4",
            "sin_expiration_date",
            "work_permit_expiration_date",
            "all_docusign_templates",
        )
        export_order = fields
        queryset = CustomUser.objects.select_related("store").prefetch_related(
            "managed_users"
        )
        queryset = CustomUser.objects.prefetch_related("processeddocsigndocument_set")

    def dehydrate_manager(self, custom_user):
        # Fetch the first related UserManager object
        user_manager = UserManager.objects.filter(user=custom_user).first()

        # Check if the user_manager exists and has a manager
        if user_manager and user_manager.manager:
            return user_manager.manager.username
        return "None"

    def dehydrate_whatsapp_consent(self, custom_user):
        consent = UserConsent.objects.filter(
            user=custom_user, consent_type="WhatsApp"
        ).first()
        return "Granted" if consent and consent.is_granted else "Not Granted"

    def dehydrate_store(self, obj):
        return obj.store.number if obj.store else "No Store Assigned"

    def dehydrate_all_docusign_templates(self, obj):
        """Retrieve and format all DocuSign template names for a user."""
        templates = ProcessedDocsignDocument.objects.filter(user=obj).values_list(
            "template_name", flat=True
        )
        if not templates:
            return "No Documents"
        return ", ".join(strip_tags(template) for template in templates)


class SINFirstDigitFilter(SimpleListFilter):
    title = "SIN First Digit"  # Display title for the filter
    parameter_name = "sin_first_digit"  # Query parameter name in the URL

    def lookups(self, request, model_admin):
        # Define filter options
        return [
            ("9", "Starts with 9"),
            ("other", "Other"),
        ]

    def queryset(self, request, queryset):
        # Apply filtering logic
        value = self.value()
        if value == "9":
            return queryset.filter(sin__startswith="9")
        elif value == "other":
            return queryset.exclude(sin__startswith="9")
        return queryset


# Inline model to display all associated DocuSign documents
class ProcessedDocusignDocumentInline(
    admin.TabularInline
):  # Or use admin.StackedInline for a different layout
    model = ProcessedDocsignDocument
    extra = 0  # Don't show empty extra forms
    fields = ("template_name", "processed_at")
    readonly_fields = (
        "template_name",
        "processed_at",
    )  # Make these fields non-editable


# CustomUser model
class CustomUserAdmin(ExportActionMixin, UserAdmin):
    resource_class = UserResource
    # Completely disables delete everywhere in admin

    def has_delete_permission(self, request, obj=None):
        return False

    # Removes “delete selected” from actions dropdown
    def get_actions(self, request):
        actions = super().get_actions(request)
        if 'delete_selected' in actions:
            del actions['delete_selected']
        return actions

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in [
            "sin_expiration_date",
            "work_permit_expiration_date",
            "work_permit_extension_date",
        ]:
            kwargs["widget"] = TextInput(
                attrs={"type": "date"}
            )  # ✅ Uses native date input, no calendar
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    # Customize the fields you want to display
    inlines = [ProcessedDocusignDocumentInline]
    list_display = (
        "is_active",
        "username",
        "full_name",
        "email",
        "store_number",
        "employer",
        "phone_number",
        "masked_sin",
        "sin_last4",
        "sin_status",
        "get_consent",
        "sin_expiration_date",
        "work_permit_expiration_date",
        "work_permit_extension_requested",
        "work_permit_extension_date",
        "all_docusign_templates",
        "last_login",
        "get_groups",
    )
    # Keep edits safe: never expose encrypted internals
    # Show masked fields as read-only; hide the encrypted fields entirely.
    readonly_fields = ("masked_sin", "sin_last4")
    exclude = ("sin_encrypted", "sin_hash")  # don’t show in forms
    list_filter = (
        "is_active",
        "groups",
        "sin_expiration_date",
        "work_permit_expiration_date",
        SINFirstDigitFilter,
    )
    ordering = ("-id",)
    autocomplete_fields = ("employer", "store")

    # A simple colored badge: Valid/Expiring/Expired/Unknown
    def sin_status(self, obj):
        # derive status from expiration date + presence of encrypted SIN
        if not obj.sin_encrypted:
            return format_html('<span style="color:#999;">Unknown</span>')
        if obj.sin_expiration_date is None:
            return format_html('<span style="color:#0a7;">Valid</span>')
        days = (obj.sin_expiration_date - obj.sin_expiration_date.today()).days \
            if hasattr(obj.sin_expiration_date, "today") else None
        if days is None:
            return format_html('<span style="color:#0a7;">Valid</span>')
        if days < 0:
            return format_html('<span style="color:#c00;">Expired</span>')
        if days <= 30:
            return format_html('<span style="color:#d97d00;">Expiring</span>')
        return format_html('<span style="color:#0a7;">Valid</span>')
    sin_status.short_description = "SIN Status"

    # ✅ Smart search: if the admin search box contains a 9-digit SIN,
    # we hash it and include exact matches; if it’s 4 digits, we match last4.
    def get_search_results(self, request, queryset, search_term):
        qs, use_distinct = super().get_search_results(request,
                                                      queryset, search_term)
        digits = normalize_digits(search_term or "")
        try:
            if len(digits) == 9:
                # exact SIN lookup via salted hash
                qs |= self.model.objects.filter(sin_hash=sin_hash(digits))
            elif len(digits) == 4:
                # support quick last-4 search
                qs |= self.model.objects.filter(sin_last4=digits)
        except Exception:
            pass
        return qs, use_distinct

    # Optional: prevent anyone from editing the masked fields
    def get_readonly_fields(self, request, obj=None):
        # keep masked/last4 readonly on change view
        ro = list(super().get_readonly_fields(request, obj))
        if obj:  # editing existing user
            for f in ("masked_sin", "sin_last4"):
                if f not in ro:
                    ro.append(f)
        return ro

    # Clean name for display

    def full_name(self, obj):
        return f"{obj.first_name} {obj.last_name}".strip() or obj.username
    full_name.short_description = "Name"
    search_fields = ("username", "email", "phone_number", "sin_last4",
                     "first_name", "last_name")
    list_editable = (
        "sin_expiration_date",
        "work_permit_expiration_date",
        "work_permit_extension_requested",
        "work_permit_extension_date",
        "phone_number",
    )
    list_per_page = 15
    # def has_delete_permission(self, request, obj=None):
    #    return False  # Disables the ability to delete users

    def store_number(self, obj):
        """Retrieve only the store number for display in the admin."""
        return obj.store.number if obj.store else "None"

    store_number.short_description = "Store"

    def get_groups(self, obj):
        return ", ".join([group.name for group in obj.groups.all()])

    get_groups.short_description = "Groups"

    def expandable_groups(self, obj):
        """Creates an expandable section for groups."""
        groups = ", ".join([group.name for group in obj.groups.all()]) or "No Groups"
        return format_html(
            '<button class="expand-btn" onclick="toggleGroups(this)">Show Groups</button>'
            '<div class="group-list" style="display: none; padding: 5px; border: 1px solid #ddd; background: #f9f9f9;">{}</div>',
            groups,
        )

    expandable_groups.short_description = "Groups"

    def all_docusign_templates(self, obj):
        """Display all template names for a user in a neat format."""
        templates = ProcessedDocsignDocument.objects.filter(user=obj).values_list(
            "template_name", flat=True
        )

        if not templates:
            return "No Documents"

        # Convert None values to empty strings
        formatted_templates = "<br>".join(filter(None, templates))

        return format_html(formatted_templates)

    all_docusign_templates.short_description = "Docusign Documents"

    def get_consent(self, obj):
        consent = UserConsent.objects.filter(user=obj, consent_type="WhatsApp").first()
        return "Granted" if consent and consent.is_granted else "Not Granted"

    get_consent.short_description = "WhatsApp Consent"

    fieldsets = list(UserAdmin.fieldsets)
    fieldsets[1] = (
        "Personal Info",
        {
            "fields": (
                "employer",
                "store",
                "first_name",
                "last_name",
                "email",
                "address",
                "address_two",
                "city",
                "state_province",
                "postal",
                "country",
                "dob",
                "sin",
                "phone_number",
            )
        },
    )

    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "employer",
                    "store",
                    "username",
                    "email",
                    "password1",
                    "password2",
                    "first_name",
                    "last_name",
                    "address",
                    "address_two",
                    "city",
                    "state_province",
                    "postal",
                    "country",
                    "dob",
                    "sin",
                    "phone_number",
                ),
            },
        ),
    )

    fieldsets.append(
        (
            "Work Permit Extension",
            {
                "fields": (
                    "work_permit_extension_requested",
                    "work_permit_extension_date",
                )
            },
        )
    )

    def save_model(self, request, obj, form, change):
        """Ensure phone_number is not overwritten when updating other fields."""
        if "phone_number" in form.cleaned_data and form.cleaned_data["phone_number"]:
            obj.phone_number = form.cleaned_data["phone_number"]
        else:
            obj.phone_number = CustomUser.objects.get(pk=obj.pk).phone_number

        if obj.work_permit_extension_requested and not obj.work_permit_extension_date:
            messages.error(
                request,
                "Work permit extension date is required when extension requested is checked.",
            )
            return

        super().save_model(request, obj, form, change)


class IncidentAdmin(admin.ModelAdmin):
    list_display = ("store", "brief_description", "eventdate")
    search_fields = ("store__number", "brief_description")
    list_filter = ("eventdate",)


class MajorIncidentAdmin(admin.ModelAdmin):
    list_display = ("store", "brief_description", "eventdate")
    search_fields = ("store__number", "brief_description")
    list_filter = ("eventdate",)


@admin.register(UserManager)
class UserManagerAdmin(admin.ModelAdmin):
    list_display = ("user", "get_manager", "user_creation_date")

    def get_manager(self, obj):
        return obj.manager.username if obj.manager else "No Manager"

    get_manager.short_description = "Manager"

    def user_creation_date(self, obj):
        return obj.user.date_joined.strftime("%Y-%m-%d")

    user_creation_date.short_description = "User Creation Date"

    search_fields = ["user__username", "manager__username"]


@admin.register(UserConsent)
class UserConsentAdmin(admin.ModelAdmin):
    list_display = ("user", "consent_type", "is_granted",
                    "granted_on", "revoked_on")
    list_filter = ("consent_type", "is_granted")
    search_fields = ("user__username",)


class StoreAdmin(admin.ModelAdmin):
    list_display = ("number", "employer", "manager")
    search_fields = ("number", "employer__name", "manager__username")
    list_filter = ("is_active", "carwash", "employer")


class AnswerInline(admin.TabularInline):
    model = Answer
    extra = 1  # Allow adding one extra answer by default
    fields = ["text", "is_correct"]


class QuestionInline(admin.StackedInline):
    model = Question
    extra = 1  # Allow adding one extra question by default
    # Do not attempt to nest inlines here; just show questions.


class QuizAdmin(admin.ModelAdmin):
    inlines = [QuestionInline]
    # Display questions inline within the Quiz admin view
    list_display = ("title", "description")
    # Display quiz title and description in the list view

    def get_queryset(self, request):
        # Preload related questions and answers to avoid multiple database hits
        return super().get_queryset(request).prefetch_related("questions__answers")

    def view_questions_and_answers(self, obj):
        # Display questions and their answers as a
        # read-only field in the list view
        questions = obj.questions.all()
        html = ""
        for question in questions:
            html += f"<strong>{question.text}</strong><br>"
            answers = question.answers.all()
            for answer in answers:
                html += f"&nbsp;&nbsp;- {answer.text} "
                f"{'(Correct)' if answer.is_correct else ''}<br>"
        return format_html(html)

    view_questions_and_answers.short_description = "Questions and Answers"


class QuestionAdmin(admin.ModelAdmin):
    inlines = [AnswerInline]  # Include AnswerInline in the QuestionAdmin
    list_display = ("text", "quiz", "display_answers")
    # Display the question text, quiz, and answers

    def display_answers(self, obj):
        answers = obj.answers.all()
        return [f"{answer.text} " for answer in answers]

    display_answers.short_description = "Answers"


class CustomTaskResultAdmin(DefaultTaskResultAdmin):
    list_display = (
        "task_id",
        "task_name",
        "periodic_task_name",
        "status",
        "short_result",
        "date_done",
        "worker",
    )

    search_fields = ("task_id", "task_name", "periodic_task_name")
    list_filter = ("status", "task_name", "periodic_task_name")

    def short_result(self, obj):
        if obj.result:
            return str(obj.result)[:75] + "..." if len(str(obj.result)) > 75 else obj.result
        return "No result"
    short_result.short_description = "Result (short)"


@admin.register(DocumentType)
class DocumentTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "description")
    search_fields = ("name",)


@admin.register(EmployeeDocument)
class EmployeeDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "document_type",
        "issue_date",
        "expiration_date",
        "is_expired",
    )
    list_filter = ("document_type", "expiration_date")
    search_fields = (
        "user__username",
        "user__email",
        "document_type__name",
        "document_number",
    )
    date_hierarchy = "expiration_date"


@admin.register(SaltLog)
class SaltLogAdmin(admin.ModelAdmin):
    list_display = (
        "store",
        "user",
        "area_salted",
        "date_salted",
        "time_salted",
        "hidden_timestamp",
    )
    list_filter = ("store", "date_salted")
    search_fields = ("store__name", "area_salted")


@admin.register(CarwashStatus)
class CarwashStatusAdmin(admin.ModelAdmin):
    list_display = ("store", "status", "reason", "date_time", "updated_by")
    list_filter = ("status", "date_time", "store")
    search_fields = ("store__number", "status", "reason")
    ordering = ("-date_time",)

    def has_delete_permission(self, request, obj=None):
        """Allow deletion of records."""
        return True  # Ensure deletion is allowed

    def has_change_permission(self, request, obj=None):
        """Ensure records can be modified."""
        return True  # Modify this if you need restrictions

    def has_add_permission(self, request):
        """Allow adding new records."""
        return True


class SMSOptOutResource(resources.ModelResource):
    """Defines the data to export"""

    first_name = resources.Field()
    last_name = resources.Field()
    phone_number = resources.Field()

    class Meta:
        model = SMSOptOut
        fields = (
            "user__first_name",
            "user__last_name",
            "user__phone_number",
            "date_added",
        )
        export_order = (
            "user__first_name",
            "user__last_name",
            "user__phone_number",
            "date_added",
        )

    def dehydrate_first_name(self, obj):
        return obj.user.first_name if obj.user else ""

    def dehydrate_last_name(self, obj):
        return obj.user.last_name if obj.user else ""

    def dehydrate_phone_number(self, obj):
        return obj.user.phone_number if obj.user else ""


class SMSOptOutForm(forms.ModelForm):
    class Meta:
        model = SMSOptOut
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Sort users by phone number (or change to 'first_name'/'last_name')
        self.fields["user"].queryset = CustomUser.objects.filter(
            is_active=True
        ).order_by("phone_number")
        self.fields["user"].label_from_instance = self.format_user_label

    def format_user_label(self, user):
        """Format how users appear in the dropdown."""
        return f"{user.first_name} {user.last_name} ({user.phone_number})"


class SMSOptOutAdmin(ExportActionMixin, admin.ModelAdmin):
    form = SMSOptOutForm
    resource_class = SMSOptOutResource
    list_display = (
        "get_first_name",
        "get_last_name",
        "get_phone",
        "user",
        "employer",
        "reason",
        "date_added",
    )
    search_fields = (
        "user__first_name",
        "user__last_name",
        "user__username",
        "user__phone_number",
        "employer__name",
    )
    ordering = (
        "user__first_name",
        "user__last_name",
        "user__phone_number",
    )  # Sorts list by phone number
    list_filter = ("employer",)  # ✅ Filter by employer in Django Admin

    autocomplete_fields = ["user"]

    def get_first_name(self, obj):
        return obj.user.first_name if obj.user else "N/A"

    get_first_name.admin_order_field = "user__first_name"
    get_first_name.short_description = "First Name"

    def get_last_name(self, obj):
        return obj.user.last_name if obj.user else "N/A"

    get_last_name.admin_order_field = "user__last_name"
    get_last_name.short_description = "Last Name"

    def get_phone(self, obj):
        return obj.user.phone_number if obj.user else "N/A"

    get_phone.admin_order_field = "user__phone_number"
    get_phone.short_description = "Phone Number"

    def get_employer(self, obj):
        return obj.employer.name if obj.employer else "N/A"

    get_employer.admin_order_field = "employer__name"
    get_employer.short_description = "Employer"


@admin.register(TenantApiKeys)
class TenantApiKeysAdmin(admin.ModelAdmin):
    list_display = (
        "employer",
        "account_sid",
        "phone_number",
        "verified_sender_email",
        "status",
        "created_at",
    )
    search_fields = (
        "employer__name",
        "service_name",
        "account_sid",
        "verified_sender_email",
    )


@admin.register(DocuSignTemplate)
class DocuSignTemplateAdmin(admin.ModelAdmin):
    list_display = ("template_name", "employer",
                    "is_new_hire_template", "created_at")
    search_fields = ("template_name",)
    list_filter = (
        "employer",
        "is_new_hire_template",
    )


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "subject", "get_employers", "is_in_app_display",
                    "sendgrid_id", "include_in_report")
    search_fields = ("name", "subject", "sendgrid_id", "employers__name")
    list_filter = ("employers", "include_in_report")
    fields = (
        "name",
        "subject",
        "html_body",
        "header_image_url",
        "header_source_url",
        "header_display_width",
        "header_space_below",
        "sendgrid_id",
        "employers",
        "include_in_report",
        "created_at",
        "updated_at",
    )
    readonly_fields = ("created_at", "updated_at")

    def get_employers(self, obj):
        """Display employers in alphabetical"""
        """order as comma-separated string."""
        sorted_employers = sorted(emp.name for emp in obj.employers.all())
        return ", ".join(sorted_employers)

    get_employers.short_description = "Employers"

    def is_in_app_display(self, obj):
        return obj.is_in_app

    is_in_app_display.boolean = True
    is_in_app_display.short_description = "In-app HTML"


@admin.register(EmployerSettings)
class EmployerSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "employer",
        "send_new_hire_file",
        "new_hire_invite_expiry_days",
    )
    list_filter = ("send_new_hire_file",)


@admin.register(Employer)
class EmployerAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "email",
        "is_active",
        "verified_sender_email",
        "toggle_active_button",
    ]
    actions = ["activate_selected", "deactivate_selected"]
    search_fields = (
        "name",
        "phone_number",
        "verified_sender_local",
        "verified_sender_email",
        "senior_contact_name",
    )
    readonly_fields = ("verified_sender_email",)  # Prevent editing full email
    list_filter = ("created", "state_province", "country")

    def toggle_active_button(self, obj):
        """Add a button to toggle employer's active status."""
        if obj.is_active:
            return format_html(
                '<a class="button" style="color:red;" href="/admin/user/employer/{}/toggle-active/">Deactivate</a>',
                obj.pk,
            )
        return format_html(
            '<a class="button" style="color:green;" href="/admin/user/employer/{}/toggle-active/">Activate</a>',
            obj.pk,
        )

    toggle_active_button.short_description = "Toggle Active"

    def activate_selected(self, request, queryset):
        """Action to activate multiple employers at once."""
        queryset.update(is_active=True)
        messages.success(request, "Selected employers have been activated.")

    activate_selected.short_description = "Activate selected employers"

    def deactivate_selected(self, request, queryset):
        """Action to deactivate multiple employers at once."""
        queryset.update(is_active=False)
        messages.warning(request, "Selected employers have been deactivated.")

    deactivate_selected.short_description = "Deactivate selected employers"

    def get_urls(self):
        """Add a custom URL to toggle active status."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:pk>/toggle-active/",
                self.admin_site.admin_view(self.toggle_active),
                name="employer-toggle-active",
            ),
        ]
        return custom_urls + urls

    def toggle_active(self, request, pk):
        """Toggle employer's active status."""
        employer = Employer.objects.get(pk=pk)
        employer.is_active = not employer.is_active
        employer.save()

        if employer.is_active:
            self.message_user(
                request, f"Employer {employer.name} is now active.",
                messages.SUCCESS
            )
        else:
            self.message_user(
                request,
                f"Employer {employer.name} has been deactivated.",
                messages.WARNING,
            )

        return redirect("/admin/user/employer/")


@admin.register(NewHireInvite)
class NewHireInviteAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "email",
        "role",
        "invited_by",
        "employer",
        "created_at",
        "expires_at",
        "used",
        "invite_link_display",
    )
    list_filter = ("used", "role", "employer", "invited_by", "created_at")
    search_fields = ("name", "email", "employer__name")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    # lets you quickly pick from large lists
    autocomplete_fields = ("employer", "invited_by")

    readonly_fields = ("token", "created_at", "invite_link_display")

    fieldsets = (
        (None, {"fields": ("name", "email", "role")}),
        ("Context", {"fields": ("employer", "invited_by", "used")}),
        ("Invite Details", {"fields": ("token", "invite_link_display",
                                       "created_at", "expires_at")}),
    )

    actions = ["mark_as_used", "mark_as_unused"]

    # ----- Link helpers -----
    def invite_link_display(self, obj):
        if not obj.pk:
            return "—"
        url = obj.get_invite_link()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)
    invite_link_display.short_description = "Invite Link"

    def invite_link_short(self, obj):
        if not obj.pk:
            return "—"
        return format_html('<a class="button" href="{}" target="_blank">Open</a>', obj.get_invite_link())
    invite_link_short.short_description = "Link"

    # ----- Actions -----
    def mark_as_used(self, request, queryset):
        count = queryset.update(used=True)
        self.message_user(request, f"Marked {count} invite(s) as used.")
    mark_as_used.short_description = "Mark selected invites as used"

    def mark_as_unused(self, request, queryset):
        count = queryset.update(used=False)
        self.message_user(request, f"Marked {count} invite(s) as unused.")
    mark_as_unused.short_description = "Mark selected invites as unused"


class SignedDocumentSingleUploadForm(forms.Form):
    user = forms.ModelChoiceField(
        queryset=CustomUser.objects.filter(is_active=True), label="Select User"
    )
    employer = forms.ModelChoiceField(
        queryset=Employer.objects.all(), label="Select Employer"
    )
    file = forms.FileField(label="Select File to Upload")


@admin.register(SignedDocumentFile)
class SignedDocumentFileAdmin(admin.ModelAdmin):
    list_display = ("file_name", "user", "employer", "uploaded_at")
    change_list_template = "admin/signed_documents_changelist.html"

    search_fields = (
        "file_name",
        "document_title",
        "user__email",
        "user__first_name",
        "user__last_name",
    )

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "upload-one/",
                self.admin_site.admin_view(self.upload_single_view),
                name="signed_document_upload_one",
            ),
        ]
        return custom_urls + urls

    def upload_single_view(self, request):
        if request.method == "POST":
            form = SignedDocumentSingleUploadForm(request.POST, request.FILES)
            if form.is_valid():
                user = form.cleaned_data["user"]
                employer = form.cleaned_data["employer"]
                file = form.cleaned_data["file"]

                folder_name = uuid.uuid4().hex[:8]
                file_path = f"DOCUMENTS/{employer.name.replace(' ', '_')}/{folder_name}/{file.name}"

                upload_to_linode_object_storage(file, file_path)

                SignedDocumentFile.objects.create(
                    user=user,
                    employer=employer,
                    envelope_id=uuid.uuid4().hex[:10],
                    file_name=file.name,
                    file_path=file_path,
                )

                self.message_user(
                    request,
                    f"Successfully uploaded file for {user}.",
                    level=messages.SUCCESS,
                )
                return redirect("..")
        else:
            form = SignedDocumentSingleUploadForm()

        context = {
            "form": form,
        }
        return render(request, "admin/signed_documents_upload_one.html",
                      context)


@admin.register(StripePlan)
class StripePlanAdmin(admin.ModelAdmin):
    list_display = ("name", "amount", "stripe_price_id")
    search_fields = ("name", "stripe_price_id")


@admin.register(StripePayment)
class StripePaymentAdmin(admin.ModelAdmin):
    list_display = ("employer", "amount", "is_paid", "payment_date")
    search_fields = ("employer__name", "stripe_customer_id",
                     "stripe_subscription_id")
    list_filter = ("is_paid",)


@admin.register(ErrorLog)
class ErrorLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "path", "method", "status_code")
    list_filter = ("status_code", "method")
    search_fields = ("path", "error_message")
    ordering = ("-timestamp",)


admin.site.register(Twimlmessages)
admin.site.register(BulkEmailSendgrid)
admin.site.register(Store, StoreAdmin)
admin.site.register(Incident, IncidentAdmin)
admin.site.register(MajorIncident, MajorIncidentAdmin)
admin.site.register(ProcessedDocsignDocument)
admin.site.register(CustomUser, CustomUserAdmin)
admin.site.register(WhatsAppTemplate)
admin.site.register(Quiz, QuizAdmin)
admin.site.register(Answer)
admin.site.register(Question, QuestionAdmin)
admin.site.register(ExternalRecipient, ExternalRecipientAdmin)
admin.site.register(SMSOptOut, SMSOptOutAdmin)
