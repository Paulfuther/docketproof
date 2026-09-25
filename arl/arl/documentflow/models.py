from django.core.exceptions import ValidationError
from django.db import models

from .constants import IMMIGRATION_STATUS_CHOICES, immigration_status_overrides_permit


# Create your models here.
class DocumentFlow(models.Model):
    employer = models.ForeignKey(
        "user.Employer",
        on_delete=models.CASCADE,
        related_name="document_flows",
    )
    name = models.CharField(max_length=255, default="New Hire Document Flow")
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.employer} - {self.name}"


class DocumentFlowStep(models.Model):
    flow = models.ForeignKey(
        DocumentFlow,
        on_delete=models.CASCADE,
        related_name="steps",
    )
    template = models.ForeignKey(
        "dsign.DocuSignTemplate",
        on_delete=models.CASCADE,
        related_name="flow_steps",
    )
    step_order = models.PositiveIntegerField()
    label = models.CharField(max_length=255, blank=True)

    is_required = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["step_order"]
        unique_together = [
            ("flow", "step_order"),
        ]
        
    def __str__(self):
        return f"{self.flow.name} - Step {self.step_order} - {self.display_name}"

    @property
    def display_name(self):
        return self.label or self.template.template_name


class SentDocuSignEnvelope(models.Model):
    STATUS_CHOICES = [
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("completed", "Completed"),
        ("declined", "Declined"),
        ("voided", "Voided"),
    ]

    employer = models.ForeignKey(
        "user.Employer",
        on_delete=models.CASCADE,
        related_name="sent_docusign_envelopes",
    )
    user = models.ForeignKey(
        "user.CustomUser",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="sent_docusign_envelopes",
    )
    template = models.ForeignKey(
        "dsign.DocuSignTemplate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_envelopes",
    )
    flow = models.ForeignKey(
        "documentflow.DocumentFlow",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_envelopes",
    )
    flow_step = models.ForeignKey(
        "documentflow.DocumentFlowStep",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_envelopes",
    )
    template_name = models.CharField(max_length=255, blank=True)
    envelope_id = models.CharField(max_length=255, unique=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="sent",
    )

    sent_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-sent_at"]

    def __str__(self):
        return f"{self.template_name or self.template} - {self.user} - {self.status}"


class SentDocuSignRecipient(models.Model):
    STATUS_CHOICES = [
        ("created", "Created"),
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("completed", "Completed"),
        ("declined", "Declined"),
        ("voided", "Voided"),
    ]

    sent_envelope = models.ForeignKey(
        "documentflow.SentDocuSignEnvelope",
        on_delete=models.CASCADE,
        related_name="recipients",
    )

    recipient_id = models.CharField(max_length=50, blank=True)
    recipient_id_guid = models.CharField(max_length=255, blank=True)

    role_name = models.CharField(max_length=100, blank=True)
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField()

    routing_order = models.PositiveIntegerField(default=1)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default="created",
    )

    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["routing_order", "id"]
        unique_together = [("sent_envelope", "recipient_id")]

    def __str__(self):
        label = self.role_name or self.email
        return f"{label} - {self.status}"


# arl/documentflow/models.py

class ImmigrationStatusEvent(models.Model):
    user = models.ForeignKey(
        "user.CustomUser",
        on_delete=models.CASCADE,
        related_name="immigration_status_events",
    )

    employer = models.ForeignKey(
        "user.Employer",
        on_delete=models.CASCADE,
        related_name="immigration_status_events",
    )

    status_type = models.CharField(
        max_length=50,
        choices=IMMIGRATION_STATUS_CHOICES
    )

    label = models.CharField(max_length=100, blank=True)
    effective_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    reference_number = models.CharField(
        max_length=100,
        blank=True,
        help_text="Application number, file number, or reference ID"
    )
    document_file = models.ForeignKey(
        "dsign.SignedDocumentFile",  # adjust app label if needed
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="immigration_status_events",
    )

    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        "user.CustomUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_immigration_status_events",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user} - {self.get_status_type_display()}"

    def validate_override_proof(self):
        """Block ranking changes that have no who / when / document-or-reference."""
        if not self.is_active:
            return
        if not immigration_status_overrides_permit(self.status_type):
            return

        if self.reference_number:
            self.reference_number = self.reference_number.strip()

        errors = {}
        has_reference = bool(self.reference_number)
        has_document = bool(self.document_file_id)
        if not has_reference and not has_document:
            message = (
                "This status changes work-permit ranking. Attach a document or "
                "enter a reference number (IRCC application or file number)."
            )
            errors["reference_number"] = message
            errors["document_file"] = message
        if not self.effective_date:
            errors["effective_date"] = (
                "Enter the date this status took effect, or the date IRCC "
                "received the application."
            )
        if not self.created_by_id:
            errors["created_by"] = "Record who entered this status."
        if errors:
            raise ValidationError(errors)

    def clean(self):
        super().clean()
        self.validate_override_proof()

    def save(self, *args, **kwargs):
        self.validate_override_proof()
        return super().save(*args, **kwargs)