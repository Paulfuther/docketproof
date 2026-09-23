from collections import defaultdict

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import models
from django.db.models import CharField, IntegerField, Value
from django.forms import RadioSelect
from django.forms.widgets import Select

from arl.msg.models import EmailTemplate, WhatsAppTemplate
from arl.user.models import CustomUser, Store

User = get_user_model()


class SMSForm(forms.Form):
    sms_message = forms.CharField(
        max_length=1000,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": "Enter a message (max 1000 characters)",
                "class": "custom-input",
            }
        ),
        help_text="",
    )

    selected_group = forms.ModelChoiceField(
        queryset=Group.objects.none(),  # Set dynamically in __init__
        required=False,
        label="Select Group to Send SMS",
        widget=RadioSelect,
    )

    selected_users = forms.ModelMultipleChoiceField(
        queryset=CustomUser.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user and user.employer:
            employer = user.employer

            # ✅ Populate groups for this employer
            self.fields["selected_group"].queryset = Group.objects.filter(
                user__employer=employer
            ).distinct()

            # ✅ Populate users for this employer
            self.fields["selected_users"].queryset = CustomUser.objects.filter(
                employer=employer, is_active=True
            ).order_by("first_name", "last_name")

    def clean_message(self):
        message = self.cleaned_data.get("message", "").strip()
        if not message:
            raise forms.ValidationError("Message cannot be empty.")
        return message


class TemplateWhatsAppForm(forms.Form):
    whatsapp_id = forms.ModelChoiceField(
        queryset=WhatsAppTemplate.objects.all(),
        required=True,
        label="Select WhatsApp Template",
    )

    selected_group = forms.ModelChoiceField(
        queryset=Group.objects.none(),  # Populated in __init__
        required=True,  # Now required
        label="Select Group to Send WhatsApp",
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user and user.employer:
            employer = user.employer

            # ✅ Only include groups related to this employer
            self.fields["selected_group"].queryset = Group.objects.filter(
                user__employer=employer
            ).distinct()


class SendGridFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="From",
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="To",
    )
    template_id = forms.ModelChoiceField(
        queryset=EmailTemplate.objects.all(),
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Template Name",
    )


class GroupedSelect(Select):
    def optgroups(self, name, value, attrs=None):
        groups = defaultdict(list)
        has_selected = False

        # Loop through EmailTemplate objects
        for index, template in enumerate(self.choices.queryset):
            option_value = template.pk
            option_label = self.choices.field.label_from_instance(template)

            # Group logic
            group_label = (
                "Generic Templates"
                if template.employers.count() == 0
                else "Employer Templates"
            )

            selected = str(option_value) in value
            option = self.create_option(
                name,
                option_value,
                option_label,
                selected,
                index,
                attrs=attrs,
            )
            groups[group_label].append(option)
            has_selected = has_selected or selected

        optgroups = [
            (label, group, index) for index, (label, group) in enumerate(groups.items())
        ]
        return optgroups


class GroupedTemplateChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        if obj.employers.exists():
            return f"{obj.name}"
        return f"{obj.name} 🌐"  # Add globe emoji for generic


class TemplateFilterForm(forms.Form):
    template_id = forms.ModelChoiceField(
        queryset=EmailTemplate.objects.none(),
        required=True,
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Template Name",
        empty_label="— Select a Template —",
    )

    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="From",
    )

    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
        label="To",
    )

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if user and hasattr(user, "employer"):
            employer = user.employer

            employer_templates = EmailTemplate.objects.filter(
                employers=employer
            ).annotate(
                sort_order=Value(0, output_field=IntegerField()),
                employer_name=Value(employer.name, output_field=CharField()),
            )

            generic_templates = EmailTemplate.objects.filter(
                employers__isnull=True
            ).annotate(
                sort_order=Value(1, output_field=IntegerField()),
                employer_name=Value("Generic", output_field=CharField()),
            )

            templates = (employer_templates | generic_templates).order_by(
                "sort_order", "name"
            )

            self.fields["template_id"].queryset = templates


class SMSLogFilterForm(forms.Form):
    start_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Start Date",
    )
    end_date = forms.DateField(
        required=False, widget=forms.DateInput(attrs={"type": "date"}), label="End Date"
    )


class CampaignSetupForm(forms.Form):
    contact_list = forms.ChoiceField(
        label="Select Contact List",
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, **kwargs):
        contact_list_choices = kwargs.pop("contact_list_choices", [])
        super().__init__(*args, **kwargs)
        self.fields["contact_list"].choices = contact_list_choices


class EmployeeSearchForm(forms.Form):
    employee = forms.ModelChoiceField(
        queryset=CustomUser.objects.filter(is_active=True),
        required=False,
        widget=forms.Select(attrs={"class": "form-control"}),
        label="Select Employee",
    )


# Form for selecting a group
class GroupSelectForm(forms.Form):
    group = forms.ModelChoiceField(
        queryset=Group.objects.all(), required=True, label="Select Group"
    )


class StoreTargetForm(forms.ModelForm):
    sales_target = forms.IntegerField(required=True, label="Sales Target")

    class Meta:
        model = Store
        fields = ["number", "sales_target"]

    def __init__(self, *args, **kwargs):
        super(StoreTargetForm, self).__init__(*args, **kwargs)
        self.fields["number"].disabled = True
        self.fields["number"].label = "Store Number"


class EmailTemplateForm(forms.ModelForm):
    class Meta:
        model = EmailTemplate
        fields = [
            "name",
            "subject",
            "header_image_url",
            "header_source_url",
            "header_display_width",
            "header_space_below",
            "html_body",
            "include_in_report",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Template name"}
            ),
            "subject": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. Welcome to {{company_name}}",
                }
            ),
            "header_image_url": forms.HiddenInput(),
            "header_source_url": forms.HiddenInput(),
            "header_display_width": forms.Select(attrs={"class": "form-select"}),
            "header_space_below": forms.Select(attrs={"class": "form-select"}),
            "html_body": forms.Textarea(
                attrs={
                    "class": "form-control font-monospace",
                    "rows": 10,
                    "placeholder": "<p>Hello {{name}}</p>",
                }
            ),
            "include_in_report": forms.CheckboxInput(
                attrs={"class": "form-check-input"}
            ),
        }
        labels = {
            "include_in_report": "Include in compliance audit",
            "header_display_width": "Header size",
            "header_space_below": "Space under header",
            "html_body": "HTML",
        }
        help_texts = {
            "name": "Shown in the Communications template picker.",
            "subject": "Supports merge fields: {{name}}, {{company_name}}, {{senior_contact_name}}.",
            "header_image_url": "Optional. Uploaded to Linode and shown at the top of the email.",
            "header_source_url": "Original photo used only to reframe the header. Not shown in the email.",
            "header_display_width": "How wide the top picture looks.",
            "header_space_below": "How much empty room sits between the top picture and your words.",
            "html_body": "Raw email HTML. Most people can leave this closed.",
            "include_in_report": "Include click/open/engagement for this template in the employee email report. One-off compose messages are not included.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from arl.msg.email_utils import (
            EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT,
            EMAIL_HEADER_SPACE_DEFAULT,
        )

        self.fields["header_display_width"].required = False
        self.fields["header_display_width"].initial = EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT
        self.fields["header_space_below"].required = False
        self.fields["header_space_below"].initial = EMAIL_HEADER_SPACE_DEFAULT

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        return name

    def clean_subject(self):
        subject = (self.cleaned_data.get("subject") or "").strip()
        if not subject:
            raise forms.ValidationError("Subject is required.")
        return subject

    def clean_html_body(self):
        html_body = (self.cleaned_data.get("html_body") or "").strip()
        if not html_body:
            raise forms.ValidationError("HTML body is required.")
        return html_body

    def clean_header_image_url(self):
        return (self.cleaned_data.get("header_image_url") or "").strip()

    def clean_header_source_url(self):
        return (self.cleaned_data.get("header_source_url") or "").strip()

    def clean_header_display_width(self):
        from arl.msg.email_utils import resolve_header_display_width

        return resolve_header_display_width(
            self.cleaned_data.get("header_display_width")
        )

    def clean_header_space_below(self):
        from arl.msg.email_utils import resolve_header_space_below

        return resolve_header_space_below(self.cleaned_data.get("header_space_below"))


class EmailForm(forms.Form):
    MODE_CHOICES = [
        ("text", "Write Custom Message"),
        ("template", "Use Template"),
    ]

    email_mode = forms.ChoiceField(
        choices=MODE_CHOICES,
        widget=forms.RadioSelect,
        initial="template",
        required=True,
        label="Choose Email Mode",
    )

    subject = forms.CharField(
        max_length=255,
        required=False,
        label="Email Subject",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Enter a subject...",
                "class": "form-control",
            }
        ),
    )

    message = forms.CharField(
        widget=forms.Textarea(
            attrs={"rows": 5, "placeholder": "Write your message here..."}
        ),
        required=False,
        label="Message Body",
    )

    sendgrid_id = forms.ModelChoiceField(
        queryset=EmailTemplate.objects.none(),
        widget=forms.RadioSelect,
        required=False,
        label="Select Template",
    )

    selected_group = forms.ModelChoiceField(
        queryset=Group.objects.none(),
        widget=forms.RadioSelect,
        required=False,
        label="Select Group",
    )

    selected_users = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        label="Select Users",
    )

    def __init__(self, *args, **kwargs):
        self.is_draft = kwargs.pop("is_draft", False)
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        if self.is_draft:
            self.fields["email_mode"].required = False
            self.fields["subject"].required = False
            self.fields["message"].required = False
            self.fields["sendgrid_id"].required = False
            self.fields["selected_group"].required = False
            self.fields["selected_users"].required = False

        if user and hasattr(user, "employer"):
            employer = user.employer

            self.fields["sendgrid_id"].queryset = (
                EmailTemplate.objects.filter(
                    models.Q(employers=employer) | models.Q(employers__isnull=True)
                )
                .distinct()
                .order_by("name")
            )
            self.fields["sendgrid_id"].label_from_instance = lambda t: (
                t.name or f"Template {t.pk}"
            )

            self.fields["selected_group"].queryset = (
                Group.objects.filter(user__employer=employer)
                .distinct()
                .order_by("name")
            )

            self.fields["selected_users"].queryset = User.objects.filter(
                employer=employer, is_active=True
            ).order_by("last_name")

            self.fields["selected_users"].label_from_instance = lambda u: (
                f"{u.last_name}, {u.first_name} – {u.email} "
                f"({u.employer.name if u.employer else 'No Employer'})"
            )

    def clean(self):
        cleaned_data = super().clean()
        # Skip full validation if draft, BUT do minimum sanity check
        if self.is_draft:
            mode = cleaned_data.get("email_mode")

            # Optional: default to "text" if user didn’t select anything
            if not mode:
                mode = "text"
                cleaned_data["email_mode"] = "text"

            if mode == "template":
                raise forms.ValidationError(
                    "Drafts can only be saved when composing a custom message."
                )

            return cleaned_data

        mode = cleaned_data.get("email_mode")
        subject = cleaned_data.get("subject")
        message = cleaned_data.get("message")
        sendgrid_id = cleaned_data.get("sendgrid_id")

        selected_group = cleaned_data.get("selected_group")
        selected_users = cleaned_data.get("selected_users")

        # Validate group/user selection
        if not selected_group and not selected_users:
            raise forms.ValidationError(
                "You must select either a group or at least one user."
            )
        if selected_group and selected_users:
            raise forms.ValidationError(
                "You cannot select both a group and individual users."
            )

        # Validate mode-based inputs
        if mode == "text":
            subject = (subject or "").strip()
            cleaned_data["subject"] = subject
            if not subject or not message:
                raise forms.ValidationError(
                    "Subject and message are required for compose (one-off) emails."
                )
        elif mode == "template":
            if not sendgrid_id:
                raise forms.ValidationError("You must select a template.")

        print("Cleaned data:", cleaned_data)
        print("Mode:", mode)
        print("Sendgrid ID:", sendgrid_id)

        return cleaned_data
