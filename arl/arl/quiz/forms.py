import json

from django import forms
from django.forms import inlineformset_factory
from django.forms.models import BaseInlineFormSet

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
    """One create/edit form. Draft saves skip submit rules.

    Store choices are limited to the signed-in user's employer.
    User and employer are not form fields, so a post cannot reassign them.
    """

    class Meta:
        model = SaltLog
        fields = [
            "store",
            "area_salted",
            "date_salted",
            "time_salted",
            "levels_ok",
            "exception_what",
            "exception_who",
            "exception_when",
        ]
        widgets = {
            "area_salted": forms.TextInput(attrs={"class": "form-control"}),
            "exception_what": forms.Textarea(
                attrs={
                    "rows": 3,
                    "class": "form-control",
                    "placeholder": "What was wrong with the salt level?",
                }
            ),
            "exception_who": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Who was told?",
                }
            ),
        }

    def __init__(self, *args, user=None, validate_submit=False, **kwargs):
        self.user = user
        self.validate_submit = validate_submit
        super().__init__(*args, **kwargs)

        employer = getattr(user, "employer", None)
        if employer is None:
            store_qs = Store.objects.none()
        else:
            store_qs = Store.objects.filter(employer=employer).order_by("number")
        self.fields["store"].queryset = store_qs
        self.fields["store"].required = False
        self.fields["store"].empty_label = "Select a store…"
        self.fields["store"].widget.attrs.setdefault("class", "form-select")

        self.fields["area_salted"].required = False
        self.fields["area_salted"].label = "Area salted"

        self.fields["date_salted"].required = False
        self.fields["date_salted"].label = "Date salted"
        self.fields["date_salted"].widget = forms.DateInput(
            attrs={"type": "date", "class": "form-control"},
            format="%Y-%m-%d",
        )
        self.fields["date_salted"].input_formats = ["%Y-%m-%d"]

        self.fields["time_salted"].required = False
        self.fields["time_salted"].label = "Time salted"
        self.fields["time_salted"].widget = forms.TimeInput(
            attrs={"type": "time", "class": "form-control"},
            format="%H:%M",
        )
        self.fields["time_salted"].input_formats = ["%H:%M", "%H:%M:%S"]

        current = ""
        if self.instance is not None and getattr(self.instance, "pk", None):
            if self.instance.levels_ok is True:
                current = "yes"
            elif self.instance.levels_ok is False:
                current = "no"
        self.fields["levels_ok"] = forms.ChoiceField(
            label="Were salt levels OK?",
            choices=(("yes", "Yes"), ("no", "No")),
            widget=forms.RadioSelect(attrs={"class": "levels-radio"}),
            required=False,
            initial=current,
        )
        # ModelForm copied the boolean onto form.initial before this field
        # existed. The radios need "yes" / "no" / "".
        self.initial["levels_ok"] = current

        self.fields["exception_what"].required = False
        self.fields["exception_what"].label = "What was wrong"
        self.fields["exception_who"].required = False
        self.fields["exception_who"].label = "Who was told"
        self.fields["exception_when"].required = False
        self.fields["exception_when"].label = "When it was reported"
        self.fields["exception_when"].widget = forms.DateTimeInput(
            attrs={
                "type": "datetime-local",
                "class": "form-control",
                "step": "60",
            },
            format="%Y-%m-%dT%H:%M",
        )
        self.fields["exception_when"].input_formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
        ]

    def clean_area_salted(self):
        return (self.cleaned_data.get("area_salted") or "").strip()

    def clean_exception_what(self):
        return (self.cleaned_data.get("exception_what") or "").strip()

    def clean_exception_who(self):
        return (self.cleaned_data.get("exception_who") or "").strip()

    def clean_levels_ok(self):
        value = self.cleaned_data.get("levels_ok")
        if value in ("", None):
            return None
        if value == "yes":
            return True
        if value == "no":
            return False
        raise forms.ValidationError("Choose yes or no.")

    def clean_store(self):
        store = self.cleaned_data.get("store")
        employer = getattr(self.user, "employer", None)
        if (
            store is not None
            and employer is not None
            and store.employer_id != employer.id
        ):
            raise forms.ValidationError("Select a store for your company.")
        return store

    def clean(self):
        cleaned = super().clean()
        if not self.validate_submit:
            return cleaned
        if "store" not in self.errors and not cleaned.get("store"):
            self.add_error("store", "Select a store.")
        if "area_salted" not in self.errors and not cleaned.get("area_salted"):
            self.add_error("area_salted", "Enter the area salted.")
        if "date_salted" not in self.errors and not cleaned.get("date_salted"):
            self.add_error("date_salted", "Enter the date salted.")
        if "time_salted" not in self.errors and not cleaned.get("time_salted"):
            self.add_error("time_salted", "Enter the time salted.")
        if "levels_ok" not in self.errors and cleaned.get("levels_ok") is None:
            self.add_error("levels_ok", "Say whether salt levels were OK.")
        elif cleaned.get("levels_ok") is False:
            if "exception_what" not in self.errors and not cleaned.get(
                "exception_what"
            ):
                self.add_error("exception_what", "Say what was wrong.")
            if "exception_who" not in self.errors and not cleaned.get("exception_who"):
                self.add_error("exception_who", "Say who was told.")
            if "exception_when" not in self.errors and not cleaned.get(
                "exception_when"
            ):
                self.add_error("exception_when", "Say when it was reported.")
        return cleaned


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


FOLLOW_UP_YES = "Y"
FOLLOW_UP_NO = "N"


def split_create_action_on(value):
    """Map stored create_action_on JSON onto the admin checkboxes.

    Returns (follow_up_on_yes, follow_up_on_no, extra_codes).
    extra_codes is None when the stored value is not a list and must be
    kept as-is unless the advanced editor replaces it.
    """
    if not isinstance(value, list):
        return False, False, None
    follow_up_on_yes = False
    follow_up_on_no = False
    extra = []
    for code in value:
        if code == FOLLOW_UP_YES:
            follow_up_on_yes = True
        elif code == FOLLOW_UP_NO:
            follow_up_on_no = True
        elif code not in extra:
            extra.append(code)
    return follow_up_on_yes, follow_up_on_no, extra


def build_create_action_on(follow_up_on_yes, follow_up_on_no, extra=None):
    """Write the checkbox state back to the create_action_on list."""
    codes = []
    if follow_up_on_yes:
        codes.append(FOLLOW_UP_YES)
    if follow_up_on_no:
        codes.append(FOLLOW_UP_NO)
    for code in extra or []:
        if code in (FOLLOW_UP_YES, FOLLOW_UP_NO) or code in codes:
            continue
        codes.append(code)
    return codes


class ChecklistTemplateItemAdminForm(forms.ModelForm):
    """Admin checkboxes for follow-up polarity.

    create_action_on stays a JSON list. These boxes are the normal way to
    edit it: Follow-up when Yes / No, plus the existing L/S flag.
    """

    follow_up_on_yes = forms.BooleanField(
        label="Follow-up when Yes",
        required=False,
        help_text=(
            "Check when Yes needs a follow-up. "
            "Leave off when Yes needs nothing else."
        ),
    )
    follow_up_on_no = forms.BooleanField(
        label="Follow-up when No",
        required=False,
        help_text=(
            "Check only when No needs a follow-up. "
            "Leave off when No is an acceptable answer."
        ),
    )
    create_action_on_raw = forms.CharField(
        label="Create action on (advanced)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "cols": 24}),
        help_text=(
            "Other answer codes, as a JSON list. "
            "Y and N are set by the checkboxes above."
        ),
    )

    class Meta:
        model = ChecklistTemplateItem
        fields = (
            "template",
            "item_code",
            "section",
            "text",
            "response_type",
            "required",
            "requires_photo",
            "allow_photo",
            "responsibility_assignable",
            "action_plan_form",
            "order",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        responsibility = self.fields["responsibility_assignable"]
        responsibility.label = "Require L or S when that follow-up applies"
        responsibility.help_text = (
            "L/S is required only for an answer that needs a follow-up. "
            "Yes does not require L unless follow-up on Yes is checked."
        )
        stored = self.instance.create_action_on
        yes, no, extra = split_create_action_on(stored)
        self._extra_codes = extra
        self._opaque_create_action_on = extra is None
        # Always, including a bound POST. Inline formsets only call save()
        # when has_changed() is true. An unchecked box is missing from POST,
        # so without this initial Django treats "was on, now off" as unchanged
        # and leaves the old create_action_on list in the database.
        self.initial["follow_up_on_yes"] = yes
        self.initial["follow_up_on_no"] = no
        self.fields["follow_up_on_yes"].initial = yes
        self.fields["follow_up_on_no"].initial = no
        if extra is None:
            self.fields["create_action_on_raw"].initial = json.dumps(stored)
            self.fields["create_action_on_raw"].help_text = (
                "This value is not a Y/N list. Edit the JSON list here, "
                'for example ["N"]. Y and N still follow the checkboxes.'
            )
        elif extra:
            self.fields["create_action_on_raw"].initial = json.dumps(extra)
        else:
            self.fields.pop("create_action_on_raw", None)

    @property
    def shows_advanced_create_action_on(self):
        return "create_action_on_raw" in self.fields

    def clean(self):
        cleaned = super().clean()
        if self._opaque_create_action_on and not self._raw_was_submitted():
            cleaned["create_action_on"] = self.instance.create_action_on
            return cleaned

        extra = [] if self._extra_codes is None else list(self._extra_codes)
        if self._raw_was_submitted():
            raw_text = (cleaned.get("create_action_on_raw") or "").strip()
            if raw_text:
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    self.add_error(
                        "create_action_on_raw",
                        'Enter a JSON list of answer codes, for example ["N"].',
                    )
                    return cleaned
                if not isinstance(parsed, list):
                    self.add_error(
                        "create_action_on_raw",
                        'Create action on must be a JSON list, for example ["N"].',
                    )
                    return cleaned
                extra = [
                    code for code in parsed if code not in (FOLLOW_UP_YES, FOLLOW_UP_NO)
                ]
            else:
                extra = []

        cleaned["create_action_on"] = build_create_action_on(
            cleaned.get("follow_up_on_yes"),
            cleaned.get("follow_up_on_no"),
            extra,
        )
        return cleaned

    def _raw_was_submitted(self):
        return (
            "create_action_on_raw" in self.fields
            and self.add_prefix("create_action_on_raw") in self.data
        )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.create_action_on = self.cleaned_data.get("create_action_on")
        if instance.create_action_on is None:
            instance.create_action_on = []
        if commit:
            instance.save()
            self.save_m2m()
        return instance


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
