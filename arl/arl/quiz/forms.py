import random
import string

import pytz
from django import forms
from django.forms import inlineformset_factory
from django.forms.models import BaseInlineFormSet
from django.utils import timezone
from django.utils.text import slugify

# checks/forms.py
from .models import (
    Answer,
    Checklist,
    ChecklistActionItem,
    ChecklistItem,
    ChecklistTemplate,
    ChecklistTemplateItem,
    Question,
    Quiz,
    SaltLog,
)

from arl.user.models import Store


class QuizForm(forms.ModelForm):
    class Meta:
        model = Quiz
        fields = ["title", "description"]


class QuestionForm(forms.ModelForm):
    class Meta:
        model = Question
        fields = ["text"]


class AnswerForm(forms.ModelForm):
    class Meta:
        model = Answer
        fields = ["text", "is_correct"]


# Inline formsets for dynamic forms


QuestionFormSet = inlineformset_factory(
    Quiz,
    Question,
    form=QuestionForm,
    extra=1,
    can_delete=True,
)
AnswerFormSet = inlineformset_factory(
    Question, Answer, form=AnswerForm, extra=1, can_delete=True
)


class SaltLogForm(forms.ModelForm):
    image_folder = forms.CharField(
        widget=forms.TextInput(attrs={"style": "display:none;"})
    )

    date_salted = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}), disabled=True
    )
    time_salted = forms.TimeField(
        widget=forms.TimeInput(attrs={"type": "time"}), disabled=True
    )

    class Meta:
        model = SaltLog
        fields = "__all__"

    def generate_random_folder(self):
        return "".join(random.choices(string.ascii_letters + string.digits, k=10))

    def create_folder(self, instance):
        if not instance.image_folder:
            slug = slugify(instance.area_salted)[:50]
            random_string = self.generate_random_folder()
            folder_name = f"{slug}-{random_string}"
            instance.image_folder = folder_name

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)

        # Set date_salted and time_salted to the current date and time, and disable them
        if self.instance.pk is None:  # Only set these for new entries
            current_datetime = timezone.now().astimezone(
                pytz.timezone("America/New_York")
            )
            self.fields["date_salted"].initial = current_datetime.date()
            self.fields["time_salted"].initial = current_datetime.time()

            # Set initial value for image_folder if it’s a new entry
            self.create_folder(self.instance)
            self.fields["image_folder"].initial = self.instance.image_folder
            self.instance.user = user  # Set user on instance directly

        if user:
            self.fields["user_employer"].initial = self.get_user_employer(user)
            self.fields["user_employer"].disabled = True

    def get_user_employer(self, user):
        return user.employer


class ChecklistTemplateForm(forms.ModelForm):
    class Meta:
        model = ChecklistTemplate
        fields = [
            "name",
            "description",
            "document_id",
            "parent_sop",
            "purpose",
            "instructions",
            "is_active",
        ]


class ChecklistTemplateItemForm(forms.ModelForm):
    class Meta:
        model = ChecklistTemplateItem
        fields = [
            "item_code",
            "section",
            "text",
            "response_type",
            "required",
            "requires_photo",
            "allow_photo",
            "responsibility_assignable",
            "create_action_on",
            "action_plan_form",
            "order",
        ]
        widgets = {
            "item_code": forms.TextInput(attrs={"class": "form-control"}),
            "section": forms.TextInput(attrs={"class": "form-control"}),
            "text": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Check description…"}
            ),
            "response_type": forms.Select(attrs={"class": "form-select"}),
            "create_action_on": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": '["N"], ["Y"], or []',
                }
            ),
            "action_plan_form": forms.TextInput(attrs={"class": "form-control"}),
            "order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }


TemplateItemFormSet = inlineformset_factory(
    ChecklistTemplate,
    ChecklistTemplateItem,
    form=ChecklistTemplateItemForm,
    extra=1,
    can_delete=True,
)


class ChecklistForm(forms.ModelForm):
    class Meta:
        model = Checklist
        fields = ["title", "notes", "store"]   # ← add store
        widgets = {
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "notes": forms.Textarea(attrs={"rows": 3, "class": "form-control"}),
            "store": forms.Select(attrs={"class": "form-select"}),
        }

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)  # pass request.user in the view
        super().__init__(*args, **kwargs)

        # Filter store choices (optional, if you scope by employer)
        qs = Store.objects.all()
        if user and hasattr(user, "employer"):
            qs = qs.filter(employer=user.employer)
        self.fields["store"].queryset = qs.order_by("number")

        # Make store REQUIRED for starting a checklist
        self.fields["store"].required = True
        self.fields["store"].empty_label = "Select a store…"

    def clean_store(self):
        store = self.cleaned_data.get("store")
        if not store:
            raise forms.ValidationError("Please select a store.")
        return store


class ChecklistItemForm(forms.ModelForm):
    # make order optional in the form; we’ll fill it in server-side
    order = forms.IntegerField(widget=forms.HiddenInput, required=False)
    action_item = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "readonly": True}),
    )
    action_required = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "class": "form-control",
                "placeholder": "What needs to be done?",
            }
        ),
    )
    who = forms.CharField(
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": "Who is responsible?"}
        ),
    )
    target_date = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={"type": "date", "class": "form-control"}),
    )
    action_note = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "class": "form-control",
                "placeholder": "Optional note",
            }
        ),
    )

    class Meta:
        model = ChecklistItem
        fields = [
            "result",
            "responsibility",
            "text_value",
            "comment",
            "order",
        ]
        widgets = {
            "result": forms.RadioSelect(attrs={"class": "answer-radio"}),
            "responsibility": forms.RadioSelect(attrs={"class": "resp-radio"}),
            "text_value": forms.TextInput(attrs={"class": "form-control"}),
            "comment": forms.Textarea(attrs={"rows": 2, "class": "form-control"}),
            "order": forms.HiddenInput(),
        }

    def __init__(self, *args, validate_submit=False, **kwargs):
        self.validate_submit = validate_submit
        super().__init__(*args, **kwargs)
        self.fields["result"].required = False
        self.fields["result"].choices = ChecklistItem.RESULT
        self.fields["responsibility"].required = False
        self.fields["responsibility"].choices = ChecklistItem.RESPONSIBILITY
        self.fields["comment"].required = False
        self.fields["comment"].label = "Comment"
        self.fields["text_value"].required = False
        self.fields["text_value"].label = "Response"

        item = self.instance
        if item and item.response_type == ChecklistTemplateItem.RESPONSE_DATE:
            self.fields["text_value"].widget = forms.DateInput(
                attrs={"type": "date", "class": "form-control"}
            )

        action = item.get_action_item() if item and item.pk else None
        if action:
            self.fields["action_item"].initial = action.action_item
            self.fields["action_required"].initial = action.action_required
            self.fields["who"].initial = action.who
            self.fields["target_date"].initial = action.target_date
            self.fields["action_note"].initial = action.note
        elif item and item.pk:
            self.fields["action_item"].initial = item.text

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned

        item = self.instance
        result = cleaned.get("result") or ""
        responsibility = cleaned.get("responsibility") or ""
        if not item.pk:
            return cleaned

        if item.creates_action(result) and not (cleaned.get("action_item") or "").strip():
            cleaned["action_item"] = item.text

        if not self.validate_submit:
            return cleaned

        self._attach_submit_errors(cleaned, item, result, responsibility)
        return cleaned

    def _attach_submit_errors(self, cleaned, item, result, responsibility):
        """Map submit_errors() onto the L/S and action-plan fields for in-row display."""
        action = None
        if item.creates_action(result):
            action = ChecklistActionItem(
                action_required=cleaned.get("action_required") or "",
                who=cleaned.get("who") or "",
                target_date=cleaned.get("target_date"),
            )
        errors = item.submit_errors(
            result=result,
            responsibility=responsibility,
            action=action,
        )
        for message in errors:
            if "L or S" in message:
                self.add_error("responsibility", message)
            elif "Y, N, or N/A" in message:
                self.add_error("result", message)
            elif "requires an answer" in message:
                self.add_error("text_value", message)
            elif "action plan" in message:
                attached = False
                if not (cleaned.get("action_required") or "").strip():
                    self.add_error("action_required", message)
                    attached = True
                if not (cleaned.get("who") or "").strip():
                    self.add_error("who", message)
                    attached = True
                if not cleaned.get("target_date"):
                    self.add_error("target_date", message)
                    attached = True
                if not attached:
                    self.add_error(None, message)
            else:
                self.add_error(None, message)

    def full_clean(self):
        super().full_clean()
        for name in self.errors:
            if name == "__all__" or name not in self.fields:
                continue
            css = self.fields[name].widget.attrs.get("class", "")
            if "is-invalid" not in css.split():
                self.fields[name].widget.attrs["class"] = f"{css} is-invalid".strip()

    def submit_error_kinds(self):
        """Classify this row's errors for the compact top-of-form summary."""
        kinds = set()
        if not self.errors:
            return kinds
        if self.errors.get("responsibility"):
            kinds.add("responsibility")
        if (
            self.errors.get("action_required")
            or self.errors.get("who")
            or self.errors.get("target_date")
        ):
            kinds.add("action")
        if self.errors.get("result"):
            kinds.add("result")
        if self.errors.get("text_value"):
            kinds.add("text")
        for message in self.non_field_errors():
            text = str(message)
            if "L or S" in text:
                kinds.add("responsibility")
            elif "action plan" in text:
                kinds.add("action")
            elif "Y, N, or N/A" in text:
                kinds.add("result")
            elif "requires an answer" in text:
                kinds.add("text")
        return kinds

    def flat_error_messages(self):
        messages = []
        for message in self.non_field_errors():
            text = str(message)
            if text not in messages:
                messages.append(text)
        for field, field_errors in self.errors.items():
            if field == "__all__":
                continue
            for message in field_errors:
                text = str(message)
                if text not in messages:
                    messages.append(text)
        return messages

    def item_label(self, index):
        item = self.instance
        code = ""
        template_item = getattr(item, "template_item", None)
        if template_item is not None:
            code = getattr(template_item, "item_code", "") or ""
        if code:
            return f"{code}"
        return f"Item {index}"

    def save_action_item(self):
        item = self.instance
        if not item.pk or self.cleaned_data.get("DELETE"):
            return None

        result = self.cleaned_data.get("result") or ""
        if not item.creates_action(result):
            ChecklistActionItem.objects.filter(checklist_item=item).delete()
            return None

        action_item_text = (self.cleaned_data.get("action_item") or item.text).strip()
        defaults = {
            "checklist": item.checklist,
            "action_item": action_item_text,
            "action_required": self.cleaned_data.get("action_required") or "",
            "who": self.cleaned_data.get("who") or "",
            "target_date": self.cleaned_data.get("target_date"),
            "note": self.cleaned_data.get("action_note") or "",
        }
        action, _created = ChecklistActionItem.objects.update_or_create(
            checklist_item=item,
            defaults=defaults,
        )
        return action


class ChecklistItemFormSetBase(BaseInlineFormSet):
    def item_error_summaries(self):
        """Compact counts + per-row links for the checklist edit error banner."""
        items = []
        need_ls = 0
        need_action = 0
        need_answer = 0
        for index, form in enumerate(self.forms, start=1):
            kinds = form.submit_error_kinds()
            if not kinds and not form.errors:
                continue
            if "responsibility" in kinds:
                need_ls += 1
            if "action" in kinds:
                need_action += 1
            if "result" in kinds or "text" in kinds:
                need_answer += 1
            items.append(
                {
                    "index": index,
                    "pk": form.instance.pk,
                    "label": form.item_label(index),
                    "text": form.instance.text,
                    "kinds": kinds,
                    "messages": form.flat_error_messages(),
                }
            )
        return {
            "rows": items,
            "count": len(items),
            "need_ls": need_ls,
            "need_action": need_action,
            "need_answer": need_answer,
        }


ChecklistItemFormSet = inlineformset_factory(
    Checklist,
    ChecklistItem,
    form=ChecklistItemForm,
    formset=ChecklistItemFormSetBase,
    extra=0,
    can_delete=False,
)
