import uuid
from pathlib import Path

# app: checks/models.py
from django.conf import settings
from django.db import models
from django.utils.text import slugify

from arl.user.models import CustomUser, Employer

User = settings.AUTH_USER_MODEL


class Quiz(models.Model):
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class Question(models.Model):
    """Yes/No quiz item.

    Correctness lives on Answer.is_correct. Follow-up is separate: a No can
    be the right answer and still need no note and no L/S.
    """

    ANSWER_YES = "Y"
    ANSWER_NO = "N"
    RESPONSIBILITY_L = "L"
    RESPONSIBILITY_S = "S"

    quiz = models.ForeignKey(Quiz, related_name="questions", on_delete=models.CASCADE)
    text = models.CharField(max_length=255)
    follow_up_on_yes = models.BooleanField(
        "Follow-up needed when answer is Yes",
        default=False,
        help_text=(
            "Check when Yes needs a follow-up. "
            "Leave off when Yes needs nothing else."
        ),
    )
    follow_up_on_no = models.BooleanField(
        "Follow-up needed when answer is No",
        default=False,
        help_text=(
            "Check only when No needs a follow-up. "
            "Leave off when No is an acceptable answer."
        ),
    )
    responsibility_assignable = models.BooleanField(
        "Require L or S when that follow-up applies",
        default=False,
        help_text=(
            "L/S is required only for an answer that needs a follow-up. "
            "Yes does not require L unless follow-up on Yes is checked."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.text

    def normalize_answer(self, answer):
        value = (answer or "").strip().lower()
        if value in ("y", "yes"):
            return self.ANSWER_YES
        if value in ("n", "no"):
            return self.ANSWER_NO
        return ""

    def follow_up_answers(self):
        codes = []
        if self.follow_up_on_yes:
            codes.append(self.ANSWER_YES)
        if self.follow_up_on_no:
            codes.append(self.ANSWER_NO)
        return codes

    @property
    def follow_up_codes(self):
        return ",".join(self.follow_up_answers())

    def needs_follow_up(self, answer):
        code = self.normalize_answer(answer)
        return bool(code and code in self.follow_up_answers())

    def needs_responsibility(self, answer):
        """L/S is required only when this answer needs a follow-up."""
        if not self.responsibility_assignable:
            return False
        return self.needs_follow_up(answer)

    def submission_errors(self, answer, follow_up="", responsibility=""):
        errors = []
        if not self.normalize_answer(answer):
            errors.append("Choose Yes or No.")
            return errors
        if self.needs_follow_up(answer) and not (follow_up or "").strip():
            errors.append("This answer requires a follow-up.")
        if self.needs_responsibility(answer):
            resp = (responsibility or "").strip().upper()
            if resp not in (self.RESPONSIBILITY_L, self.RESPONSIBILITY_S):
                errors.append("Choose L or S responsibility.")
        return errors

    def selected_is_correct(self, selected_answer):
        correct_answer = self.answers.filter(is_correct=True).first()
        if not correct_answer or selected_answer is None:
            return False
        if selected_answer == correct_answer.text.lower():
            return True
        selected_code = self.normalize_answer(selected_answer)
        return bool(
            selected_code
            and selected_code == self.normalize_answer(correct_answer.text)
        )


class Answer(models.Model):
    question = models.ForeignKey(
        Question, related_name="answers", on_delete=models.CASCADE
    )
    text = models.CharField(max_length=255)
    is_correct = models.BooleanField(default=False)

    def __str__(self):
        return self.text


class SaltLog(models.Model):
    user = models.ForeignKey(
        CustomUser, null=True, on_delete=models.CASCADE, related_name="salt_log"
    )
    store = models.ForeignKey("user.Store", on_delete=models.CASCADE)
    area_salted = models.CharField(max_length=255)
    date_salted = models.DateField(null=True)
    time_salted = models.TimeField(null=True)  # Add time field
    hidden_timestamp = models.DateTimeField(auto_now_add=True)
    image_folder = models.CharField(max_length=255, null=True)
    user_employer = models.ForeignKey(Employer, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"Salt Log {self.pk} for {self.user.first_name} {self.user.last_name}"


def checklist_photo_upload_to(instance, filename):
    # /checklists/<checklist_id>/<item_uuid>/<originalname>
    return str(
        Path("checklists")
        / str(instance.checklist_id or "unassigned")
        / str(instance.uuid)
        / filename
    )


class ChecklistTemplate(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    document_id = models.CharField(
        max_length=80,
        blank=True,
        db_index=True,
        help_text="External document id, e.g. ENMCDS840-6.3",
    )
    parent_sop = models.CharField(max_length=80, blank=True)
    purpose = models.TextField(blank=True)
    instructions = models.TextField(blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_checklist_templates",
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


def default_create_action_on():
    return ["N"]


class ChecklistTemplateItem(models.Model):
    RESPONSE_YES_NO_NA = "yes_no_na"
    RESPONSE_TEXT = "text"
    RESPONSE_DATE = "date"
    RESPONSE_TYPES = (
        (RESPONSE_YES_NO_NA, "Yes / No / N/A"),
        (RESPONSE_TEXT, "Text"),
        (RESPONSE_DATE, "Date"),
    )

    template = models.ForeignKey(
        ChecklistTemplate, on_delete=models.CASCADE, related_name="items"
    )
    item_code = models.CharField(max_length=40, blank=True)
    section = models.CharField(max_length=200, blank=True)
    text = models.CharField(max_length=500)
    response_type = models.CharField(
        max_length=20,
        choices=RESPONSE_TYPES,
        default=RESPONSE_YES_NO_NA,
    )
    required = models.BooleanField(default=True)
    requires_photo = models.BooleanField(default=False)
    allow_photo = models.BooleanField(default=True)
    responsibility_assignable = models.BooleanField(
        default=True,
        help_text="Show L/S responsibility when the answer is Y or N.",
    )
    create_action_on = models.JSONField(
        default=default_create_action_on,
        blank=True,
        help_text='Answer codes that open a 6.4 action plan, e.g. ["N"].',
    )
    action_plan_form = models.CharField(max_length=80, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.template.name} » {self.text}"

    @property
    def creates_action_on_no(self):
        return "N" in (self.create_action_on or [])

    @property
    def is_yes_no_na(self):
        return self.response_type == self.RESPONSE_YES_NO_NA


class Checklist(models.Model):
    STATUS = (
        ("draft", "Draft"),
        ("submitted", "Submitted"),
        ("completed", "Completed (PDF generated)"),
    )

    template = models.ForeignKey(
        ChecklistTemplate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="checklists",
    )
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, editable=False)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="created_checklists"
    )
    submitted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_checklists",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="draft")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    store = models.ForeignKey(
        "user.Store",     # or "yourapp.Store" — use the correct app label
        on_delete=models.PROTECT,
        null=True,          # set null=True for the first migration to avoid breaking existing rows
        blank=True,
        related_name="checklists",
    )
    pdf_file = models.FileField(
        upload_to="checklists/pdfs/", null=True, blank=True
    )  # optionally swap to Linode/S3 Storage

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title) or "checklist"
            self.slug = f"{base}-{uuid.uuid4().hex[:8]}"
        super().save(*args, **kwargs)

    def open_action_items(self):
        return self.action_items.filter(status=ChecklistActionItem.STATUS_OPEN)

    def submit_errors(self):
        errors = []
        items = self.items.select_related("template_item", "action_item")
        for item in items:
            for message in item.submit_errors():
                errors.append(f"{item.text}: {message}")
        return errors

    def can_submit(self):
        return not self.submit_errors()


def action_photo_upload_to(instance, filename):
    checklist_id = (
        instance.checklist_id
        or getattr(instance.checklist_item, "checklist_id", None)
        or "unassigned"
    )
    return str(
        Path("checklists")
        / str(checklist_id)
        / "actions"
        / str(instance.pk or "new")
        / filename
    )


class ChecklistItem(models.Model):
    RESULT_YES = "yes"
    RESULT_NO = "no"
    RESULT_NA = "na"
    RESULT = (
        (RESULT_YES, "Y"),
        (RESULT_NO, "N"),
        (RESULT_NA, "N/A"),
    )
    RESULT_TO_ANSWER = {
        RESULT_YES: "Y",
        RESULT_NO: "N",
        RESULT_NA: "N/A",
    }
    ANSWER_TO_RESULT = {
        "Y": RESULT_YES,
        "N": RESULT_NO,
        "N/A": RESULT_NA,
        "yes": RESULT_YES,
        "no": RESULT_NO,
        "na": RESULT_NA,
    }
    RESPONSIBILITY_L = "L"
    RESPONSIBILITY_S = "S"
    RESPONSIBILITY = (
        (RESPONSIBILITY_L, "L — Associate / direct employer / franchisee"),
        (RESPONSIBILITY_S, "S — Supplier / owner / franchisor / gas company"),
    )

    checklist = models.ForeignKey(
        Checklist, on_delete=models.CASCADE, related_name="items"
    )
    template_item = models.ForeignKey(
        ChecklistTemplateItem, on_delete=models.SET_NULL, null=True, blank=True
    )
    uuid = models.UUIDField(default=uuid.uuid4, editable=False)
    text = models.CharField(max_length=500)  # store a copy for audit
    section = models.CharField(max_length=200, blank=True)
    result = models.CharField(
        max_length=5, choices=RESULT, blank=True, default=""
    )
    responsibility = models.CharField(
        max_length=1, choices=RESPONSIBILITY, blank=True, default=""
    )
    text_value = models.TextField(blank=True)
    comment = models.TextField(blank=True)
    photo = models.ImageField(
        upload_to=checklist_photo_upload_to,
        blank=True,
        null=True,
        max_length=512,
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"{self.checklist.title} » {self.text[:40]}"

    @property
    def answer(self):
        return self.RESULT_TO_ANSWER.get(self.result, "")

    @property
    def response_type(self):
        if self.template_item_id and self.template_item:
            return self.template_item.response_type
        return ChecklistTemplateItem.RESPONSE_YES_NO_NA

    @property
    def is_yes_no_na(self):
        return self.response_type == ChecklistTemplateItem.RESPONSE_YES_NO_NA

    @property
    def is_required(self):
        if self.template_item_id and self.template_item:
            return self.template_item.required
        return True

    @property
    def responsibility_assignable(self):
        if self.template_item_id and self.template_item:
            return self.template_item.responsibility_assignable
        return self.is_yes_no_na

    def create_action_answers(self):
        if self.template_item_id and self.template_item is not None:
            return list(self.template_item.create_action_on or [])
        if self.is_yes_no_na:
            return ["N"]
        return []

    def creates_action(self, result=None):
        value = self.result if result is None else result
        answer = self.RESULT_TO_ANSWER.get(value, "")
        return bool(answer and answer in self.create_action_answers())

    def needs_responsibility(self, result=None):
        value = self.result if result is None else result
        return (
            self.responsibility_assignable
            and value in (self.RESULT_YES, self.RESULT_NO)
        )

    def get_action_item(self):
        try:
            return self.action_item
        except ChecklistActionItem.DoesNotExist:
            return None

    def submit_errors(self, result=None, responsibility=None, action=None):
        """Return human-readable reasons this item cannot be submitted."""
        value = self.result if result is None else result
        resp = self.responsibility if responsibility is None else responsibility
        errors = []
        if not self.is_yes_no_na:
            if self.is_required and not (self.text_value or "").strip():
                errors.append("This item requires an answer.")
            return errors
        if self.is_required and value not in (
            self.RESULT_YES,
            self.RESULT_NO,
            self.RESULT_NA,
        ):
            errors.append("Choose Y, N, or N/A.")
        if self.needs_responsibility(value) and resp not in (
            self.RESPONSIBILITY_L,
            self.RESPONSIBILITY_S,
        ):
            errors.append("Choose L or S responsibility.")
        if self.creates_action(value):
            action = action if action is not None else self.get_action_item()
            if action is None or not action.is_ready_for_submit():
                errors.append(
                    "N requires an action plan (action required, who, and when)."
                )
        return errors


class ChecklistActionItem(models.Model):
    STATUS_OPEN = "open"
    STATUS_DONE = "done"
    STATUS = (
        (STATUS_OPEN, "Open"),
        (STATUS_DONE, "Done"),
    )

    checklist = models.ForeignKey(
        Checklist, on_delete=models.CASCADE, related_name="action_items"
    )
    checklist_item = models.OneToOneField(
        ChecklistItem,
        on_delete=models.CASCADE,
        related_name="action_item",
    )
    action_item = models.CharField(max_length=500)
    action_required = models.TextField(blank=True)
    who = models.CharField(max_length=200, blank=True)
    target_date = models.DateField(null=True, blank=True)
    completion_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default=STATUS_OPEN)
    note = models.TextField(blank=True)
    photo = models.ImageField(
        upload_to=action_photo_upload_to,
        blank=True,
        null=True,
        max_length=512,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["checklist_item__order", "id"]

    def __str__(self):
        return f"Action: {self.action_item[:40]}"

    def is_ready_for_submit(self):
        return bool(
            (self.action_required or "").strip()
            and (self.who or "").strip()
            and self.target_date
        )
