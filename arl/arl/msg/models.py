from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import models
from django.utils import timezone
from django.utils.timezone import now
from arl.user.models import CustomUser, Employer
from arl.msg.email_utils import (
    EMAIL_HEADER_DISPLAY_WIDTH_CHOICES,
    EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT,
    EMAIL_HEADER_SPACE_CHOICES,
    EMAIL_HEADER_SPACE_DEFAULT,
    EMAIL_SOURCE_CHOICES,
    email_source_label,
)

from arl.bucket.helpers import conn, upload_to_linode_object_storage


class Twimlmessages(models.Model):
    twimlname = models.CharField(max_length=100)
    twimlid = models.CharField(max_length=100)

    def __str__(self):
        return "%r" % (self.twimlname)


class BulkEmailSendgrid(models.Model):
    templatename = models.CharField(max_length=100)
    templateid = models.CharField(max_length=100)

    def __str__(self):
        return "%r" % (self.templatename)


class EmailEvent(models.Model):
    employer = models.ForeignKey(
        Employer, null=True, blank=True, on_delete=models.SET_NULL
    )
    user = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, null=True, blank=True
    )
    username = models.CharField(max_length=150, default="unknown")
    email = models.EmailField()
    event = models.CharField(max_length=10)
    ip = models.GenericIPAddressField()
    sg_event_id = models.CharField(max_length=255)
    sg_message_id = models.CharField(max_length=255)
    sg_template_id = models.CharField(max_length=255)
    sg_template_name = models.CharField(max_length=255)
    source = models.CharField(
        max_length=20,
        choices=EMAIL_SOURCE_CHOICES,
        blank=True,
        default="",
        help_text="in_app, sendgrid, or compose — from unique args when SendGrid has no template.",
    )
    app_template_id = models.PositiveIntegerField(
        blank=True,
        null=True,
        help_text="EmailTemplate.pk for in-app/legacy template sends (audit join).",
    )
    subject = models.CharField(max_length=255, blank=True, null=True)
    timestamp = models.DateTimeField()
    url = models.URLField()
    useragent = models.TextField(max_length=1000)

    def __str__(self):
        return f"{self.email} - {self.event}"

    @property
    def source_label(self):
        return email_source_label(self.source, sendgrid_id=self.sg_template_id)

    class Meta:
        ordering = ["-timestamp"]


class SmsLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    level = models.CharField(max_length=10)
    message = models.TextField()

    def __str__(self):
        return f"{self.timestamp} - {self.level}: {self.message}"


class EmailLog(models.Model):
    employer = models.ForeignKey(
        "user.Employer", on_delete=models.CASCADE, related_name="email_logs"
    )
    sender_email = models.EmailField(help_text="The verified sender email used")
    template_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="SendGrid template ID when one was used (empty for in-app HTML)",
    )
    template_name = models.CharField(max_length=255, blank=True, null=True)
    source = models.CharField(
        max_length=20,
        choices=EMAIL_SOURCE_CHOICES,
        blank=True,
        default="",
        help_text="in_app, sendgrid, or compose",
    )
    subject = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Resolved subject that was sent (user input or template subject)",
    )
    sent_at = models.DateTimeField(default=now)
    status = models.CharField(
        max_length=20,
        choices=[("SUCCESS", "Success"), ("FAILED", "Failed")],
        default="SUCCESS",
    )
    error_message = models.TextField(
        blank=True, null=True, help_text="Error details if failed"
    )

    def __str__(self):
        return f"EmailLog {self.id} - {self.employer.name} - {self.sent_at.strftime('%Y-%m-%d %H:%M')}"

    @property
    def source_label(self):
        return email_source_label(self.source, sendgrid_id=self.template_id)


class EmailTemplate(models.Model):
    employers = models.ManyToManyField(
        Employer, blank=True, related_name="email_templates"
    )
    name = models.CharField(
        max_length=100,
        null=True,
        help_text="The name of the email template (e.g., 'New Hire Onboarding')",
    )  # ✅ Allow same name for multiple employers
    sendgrid_id = models.TextField(
        blank=True,
        default="",
        help_text="Optional legacy SendGrid dynamic template ID. Leave blank for in-app templates; SendGrid is used as send transport only.",
    )
    subject = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Email subject. Supports {{name}}, {{company_name}}, {{senior_contact_name}}.",
    )
    html_body = models.TextField(
        blank=True,
        default="",
        help_text="In-app HTML body. Supports {{name}}, {{company_name}}, {{senior_contact_name}}. Image URLs should be public (Linode).",
    )
    header_image_url = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Optional public header image URL (Linode). Shown at the top of preview and outbound HTML.",
    )
    header_source_url = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Uncropped header original used to reframe the banner. Not sent in the email.",
    )
    header_display_width = models.PositiveSmallIntegerField(
        default=EMAIL_HEADER_DISPLAY_WIDTH_DEFAULT,
        choices=EMAIL_HEADER_DISPLAY_WIDTH_CHOICES,
        help_text="How wide the header appears in the email, in pixels. Not stretched to the full column.",
    )
    header_space_below = models.PositiveSmallIntegerField(
        default=EMAIL_HEADER_SPACE_DEFAULT,
        choices=EMAIL_HEADER_SPACE_CHOICES,
        help_text="Space between the header picture and the message. Outlook uses a spacer row.",
    )
    include_in_report = models.BooleanField(
        default=False,
        help_text="Include this template in the employee compliance / click-engagement report.",
    )
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    def __str__(self):
        names = ", ".join(emp.name for emp in self.employers.all())
        label = self.name or f"Template {self.pk}"
        return f"{label} - {names}" if names else label

    @property
    def is_in_app(self):
        return bool((self.html_body or "").strip())

    def resolved_subject(self):
        return (self.subject or self.name or "").strip()


class WhatsAppTemplate(models.Model):
    name = models.CharField(
        max_length=255, help_text="Friendly name of the WhatsApp template"
    )
    content_sid = models.CharField(
        max_length=255,
        unique=True,
        help_text="The unique identifier for the template (e.g., Twilio Content SID)",
    )
    description = models.TextField(
        blank=True, help_text="Description of what this template is used for"
    )

    def __str__(self):
        return f"{self.name} - {self.content_sid}"

    class Meta:
        verbose_name = "WhatsApp Template"
        verbose_name_plural = "WhatsApp Templates"


class UserConsent(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="consents"
    )
    consent_type = models.CharField(
        max_length=100, help_text="Type of consent, e.g., 'SMS', 'Email'"
    )
    is_granted = models.BooleanField(
        default=False, help_text="Whether the user has granted consent"
    )
    granted_on = models.DateTimeField(
        null=True, blank=True, help_text="When the consent was granted"
    )
    revoked_on = models.DateTimeField(
        null=True, blank=True, help_text="When the consent was revoked, if applicable"
    )

    def __str__(self):
        status = "Granted" if self.is_granted else "Revoked"
        return f"{self.user.username} - {self.consent_type} - {status}"

    class Meta:
        unique_together = (
            "user",
            "consent_type",
        )  # Ensures uniqueness for the combination of user and consent type


class Message(models.Model):
    sender = models.CharField(max_length=20)
    receiver = models.CharField(max_length=20)
    message_status = models.CharField(max_length=20)
    username = models.CharField(max_length=100, blank=True, null=True)
    action_time = models.DateTimeField(default=timezone.now)
    template_used = models.BooleanField(default=False)
    message_type = models.CharField(
        max_length=10, default="WhatsApp"
    )  # 'WhatsApp' or 'SMS'

    def __str__(self):
        return f"{self.message_type} message from {self.sender} to {self.receiver}, status {self.message_status}, at {self.action_time}"


class EmailList(models.Model):
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)


class ComplianceFile(models.Model):
    title = models.CharField(max_length=255, default="Weekly Compliance Policy")
    file = models.FileField(upload_to="compliance/")
    s3_key = models.CharField(max_length=512, blank=True, null=True)
    presigned_url = models.URLField(max_length=1024, blank=True, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        if self.is_active:
            ComplianceFile.objects.exclude(pk=self.pk).update(is_active=False)

        super().save(*args, **kwargs)

        # Upload and generate URL
        if self.file and not self.s3_key:
            unique_key = f"compliance/{uuid4()}_{self.file.name}"

            # Upload to Linode (your unchanged helper)
            upload_to_linode_object_storage(self.file.file, unique_key)
            self.s3_key = unique_key

            # ✅ Make it public
            bucket = conn.get_bucket(settings.LINODE_BUCKET_NAME)
            key = bucket.get_key(unique_key)
            if key:
                key.set_acl("public-read")

            # ✅ Public URL (not presigned)
            from urllib.parse import quote

            self.presigned_url = f"https://{settings.LINODE_BUCKET_NAME}.{settings.LINODE_REGION}/{quote(unique_key)}"

            super().save(update_fields=["s3_key", "presigned_url"])

    def __str__(self):
        return f"{self.title} - {self.uploaded_at.strftime('%Y-%m-%d')}"

    class Meta:
        ordering = ["-uploaded_at"]


# -- We are keeping this for now --
# -- This decision was made March 11, 2026 -- 
# -- It has been replaced by the new models --
class ShortenedSMSLog(models.Model):
    EVENT_TYPES = [
        ("sent", "Sent"),
        ("delivered", "Delivered"),
        ("click", "Clicked"),
        ("failed", "Failed"),
    ]

    employer = models.ForeignKey(
        Employer, null=True, blank=True, on_delete=models.SET_NULL
    )
    sms_sid = models.CharField(max_length=64)
    to = models.CharField(max_length=20)
    from_number = models.CharField(max_length=20)
    messaging_service_sid = models.CharField(max_length=64)
    account_sid = models.CharField(max_length=64)
    link = models.URLField(max_length=1000, blank=True, null=True)
    click_time = models.DateTimeField(blank=True, null=True)
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    user_agent = models.TextField(blank=True, null=True)
    user = models.ForeignKey(
        CustomUser, null=True, blank=True, on_delete=models.SET_NULL
    )
    error_code = models.CharField(max_length=10, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)
    body = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.event_type.upper()} - {self.to}"


class DraftEmail(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    employer = models.ForeignKey("user.Employer", on_delete=models.CASCADE)
    mode = models.CharField(
        max_length=20, choices=[("text", "Text"), ("template", "Template")]
    )
    subject = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    sendgrid_template = models.ForeignKey(
        EmailTemplate, null=True, blank=True, on_delete=models.SET_NULL
    )
    selected_group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.SET_NULL
    )
    selected_users = models.ManyToManyField(
        settings.AUTH_USER_MODEL, related_name="draft_recipients", blank=True
    )
    attachment_urls = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


# --- New Shortened SMS log models ---
# --- Created Mar 11 2026 ---
# --- This will eventually replace the SMSlog model from above ---


class ShortenedSMSMessage(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_SENT = "sent"
    STATUS_DELIVERED = "delivered"
    STATUS_CLICKED = "clicked"
    STATUS_FAILED = "failed"
    STATUS_UNDELIVERED = "undelivered"

    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_SENT, "Sent"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_CLICKED, "Clicked"),
        (STATUS_FAILED, "Failed"),
        (STATUS_UNDELIVERED, "Undelivered"),
    ]

    MESSAGE_TYPE_CHOICES = [
        ("secure_link", "Secure Link"),
        ("new_hire", "New Hire"),
        ("quiz", "Quiz"),
        ("document", "Document"),
        ("notification", "Notification"),
        ("incident", "Incident"),
        ("other", "Other"),
    ]

    employer = models.ForeignKey(
        Employer,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="shortened_sms_messages",
    )
    user = models.ForeignKey(
        CustomUser,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="shortened_sms_messages",
    )

    sms_sid = models.CharField(max_length=64, unique=True, db_index=True)
    to = models.CharField(max_length=20, db_index=True)
    from_number = models.CharField(max_length=20, blank=True, null=True)

    messaging_service_sid = models.CharField(max_length=64, blank=True, null=True)
    account_sid = models.CharField(max_length=64, blank=True, null=True)

    body = models.TextField(blank=True, null=True)
    body_preview = models.CharField(max_length=255, blank=True, null=True)

    link = models.URLField(max_length=1000, blank=True, null=True)
    short_code = models.CharField(max_length=100, blank=True, null=True, db_index=True)

    message_type = models.CharField(
        max_length=50,
        choices=MESSAGE_TYPE_CHOICES,
        default="secure_link",
        blank=True,
        null=True,
        db_index=True,
    )

    # optional context back into your app
    context_model = models.CharField(max_length=100, blank=True, null=True)
    context_id = models.PositiveIntegerField(blank=True, null=True)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_SENT,
        db_index=True,
    )

    click_count = models.PositiveIntegerField(default=0)

    sent_at = models.DateTimeField(blank=True, null=True, db_index=True)
    delivered_at = models.DateTimeField(blank=True, null=True)
    clicked_at = models.DateTimeField(blank=True, null=True)
    failed_at = models.DateTimeField(blank=True, null=True)

    error_code = models.CharField(max_length=20, blank=True, null=True)
    error_message = models.TextField(blank=True, null=True)

    provider_payload = models.JSONField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["employer", "created_at"]),
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["to", "created_at"]),
        ]

    def __str__(self):
        return f"{self.sms_sid} - {self.to}"

    @property
    def was_clicked(self):
        return self.click_count > 0

    def save(self, *args, **kwargs):
        if self.body and not self.body_preview:
            self.body_preview = self.body[:255]
        super().save(*args, **kwargs)


class ShortenedSMSEvent(models.Model):
    EVENT_SENT = "sent"
    EVENT_DELIVERED = "delivered"
    EVENT_CLICK = "click"
    EVENT_FAILED = "failed"
    EVENT_UNDELIVERED = "undelivered"

    EVENT_TYPES = [
        (EVENT_SENT, "Sent"),
        (EVENT_DELIVERED, "Delivered"),
        (EVENT_CLICK, "Clicked"),
        (EVENT_FAILED, "Failed"),
        (EVENT_UNDELIVERED, "Undelivered"),
    ]

    message = models.ForeignKey(
        ShortenedSMSMessage,
        on_delete=models.CASCADE,
        related_name="events",
    )

    event_type = models.CharField(max_length=20, choices=EVENT_TYPES, db_index=True)
    event_time = models.DateTimeField(default=timezone.now, db_index=True)

    user_agent = models.TextField(blank=True, null=True)
    error_code = models.CharField(max_length=20, blank=True, null=True)

    payload = models.JSONField(blank=True, null=True)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-event_time"]
        indexes = [
            models.Index(fields=["event_type", "event_time"]),
            models.Index(fields=["message", "event_time"]),
        ]

    def __str__(self):
        return f"{self.message.sms_sid} - {self.event_type}"

# --- End ---
