from datetime import timedelta
from typing import Optional

from django.contrib.auth.models import AbstractUser, Group
from django.core.validators import (
    MaxLengthValidator,
    MinLengthValidator,
    RegexValidator,
)
from django.db import models
from django_countries.fields import CountryField
from phonenumber_field.modelfields import PhoneNumberField
from django.utils.timezone import now
from django.utils.crypto import get_random_string
from django.conf import settings
from arl.utils.crypto import sin_decrypt


class Employer(models.Model):
    name = models.CharField(max_length=100)
    # Add any additional fields for the employer
    email = models.EmailField(null=True, blank=True)  # No unique=True
    address = models.CharField(max_length=100, null=True)
    address_two = models.CharField(max_length=100, null=True, blank=True)
    city = models.CharField(max_length=100, null=True)
    state_province = models.CharField(max_length=100, null=True)
    country = CountryField(null=True)
    phone_number = PhoneNumberField(null=True)
    senior_contact_name = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="The senior contact person for this employer (e.g., HR Manager, Director)."
    )
    created = models.DateTimeField(auto_now_add=True, null=True)
    updated = models.DateTimeField(auto_now=True, null=True)
    verified_sender_local = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        help_text="Enter only the part before '@yourdomain.com'"
    )
    verified_sender_email = models.EmailField(
        max_length=255,
        unique=True,
        blank=True,
        null=True,
        help_text="This is the full email generated based on your input."
    )
    is_active = models.BooleanField(default=False)
    subscription_id = models.CharField(max_length=255, blank=True, null=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Store(models.Model):
    number = models.IntegerField()
    carwash = models.BooleanField(default=False)
    address = models.CharField(max_length=100, null=True)
    address_two = models.CharField(max_length=100, blank=True)
    city = models.CharField(max_length=100, null=True)
    province = models.CharField(max_length=100, null=True)
    country = CountryField(null=True)
    phone_number = PhoneNumberField(null=True)
    created = models.DateTimeField(auto_now_add=True, null=True)
    updated = models.DateTimeField(auto_now=True, null=True)
    is_active = models.BooleanField(default=True)
    employer = models.ForeignKey(
        Employer, on_delete=models.CASCADE, null=True, related_name="stores"
    )
    manager = models.ForeignKey('CustomUser', on_delete=models.SET_NULL,
                                null=True, blank=True,
                                related_name='managed_stores')

    def __str__(self):
        if self.employer:
            manager_name = self.manager.first_name if self.manager else "No Manager"
            return f"Store {self.number} - {self.address} - {self.city} - {self.province} - Manager: {manager_name}"
        else:
            return f"Store {self.number}"


class CustomUser(AbstractUser):
    phone_number = PhoneNumberField(
        blank=False,
        null=False,
        help_text="Enter a phone number")
    sin = models.CharField(
        max_length=9,
        validators=[
            MinLengthValidator(9),
            MaxLengthValidator(9),
            RegexValidator(r"^\d{9}$", "SIN number must be 9 digits"),
        ],
        null=True,
    )
    # Work Permit Extension Tracking
    work_permit_extension_requested = models.BooleanField(default=False)

    work_permit_extension_date = models.DateField(
        null=True,
        blank=True,
        help_text="Date extension was submitted to IRCC"
    )

    # NEW encrypted fields
    sin_encrypted = models.TextField(null=True, blank=True)
    sin_last4 = models.CharField(max_length=4, null=True, blank=True,
                                 db_index=True)
    sin_hash = models.CharField(max_length=64, null=True, blank=True,
                                db_index=True)
    sin_expiration_date = models.DateField(blank=True, null=True, verbose_name="SIN Expiration Date")
    work_permit_expiration_date = models.DateField(
        blank=True, null=True,
        verbose_name="Work Permit Expiration Date")
    dob = models.DateField(blank=True, null=True, verbose_name="Date of Birth")
    address = models.CharField(max_length=100, null=True)
    address_two = models.CharField(max_length=100, null=True, blank=True)
    city = models.CharField(max_length=100, null=True)
    state_province = models.CharField(max_length=100, null=True)
    country = CountryField(null=True)
    postal = models.CharField(max_length=7, null=True)
    store = models.ForeignKey(Store, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='users')
    employer = models.ForeignKey(Employer,
                                 on_delete=models.SET_NULL, null=True)
    store = models.ForeignKey(Store, on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='users')

    def __str__(self):
        return self.first_name

    # Decrypt on demand (never render directly in templates)
    @property
    def sin_plain(self) -> Optional[str]:
        try:
            return sin_decrypt(self.sin_encrypted) if self.sin_encrypted else None
        except Exception:
            return None

    def masked_sin(self) -> str:
        return f"*** *** {self.sin_last4}" if self.sin_last4 else "*********"

    @property
    def is_docusign(self):
        return self.groups.filter(name="docusign").exists()

    @property
    def is_dropbox(self):
        return self.groups.filter(name="dropbox").exists()

    @property
    def is_incident_form(self):
        return self.groups.filter(name="incident_form").exists()

    @property
    def is_comms(self):
        return self.groups.filter(name="comms").exists()

    @property
    def is_template_email(self):
        return self.groups.filter(name="template_email").exists()

    @property
    def is_storage(self):
        return self.groups.filter(name="storage").exists()


class SMSOptOut(models.Model):
    user = models.OneToOneField(
        CustomUser,
        on_delete=models.CASCADE,
        related_name="sms_opt_out"
    )
    employer = models.ForeignKey(
        "user.Employer",
        on_delete=models.CASCADE,
        related_name="sms_opt_outs",
        null=True,  # Allow null for existing records before employer was added
        blank=True,  # Allow blank values for optional selection
        help_text="Employer associated with this opt-out."
    )
    reason = models.TextField(blank=True, null=True)
    date_added = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        """Ensure employer is assigned based on user, if not manually set."""
        if not self.employer and self.user and self.user.employer:
            self.employer = self.user.employer
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} opted out of SMS ({self.employer.name if self.employer else 'No Employer'})"


class UserManager(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE,
                                related_name='user_manager_profile')
    manager = models.ForeignKey(
        CustomUser, on_delete=models.CASCADE, related_name="managed_users"
    )

    def __str__(self):
        return self.user.username


class ErrorLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True)
    path = models.CharField(max_length=255)
    method = models.CharField(max_length=10)
    status_code = models.IntegerField()
    error_message = models.TextField()

    def __str__(self):
        return f"[{self.status_code}] {self.path} at {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"


class ExternalRecipient(models.Model):
    first_name = models.CharField(max_length=50, blank=True, null=True)
    last_name = models.CharField(max_length=50, blank=True, null=True)
    company = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(unique=True)
    group = models.ForeignKey(Group, on_delete=models.SET_NULL,
                              null=True, blank=True)
    is_active = models.BooleanField(default=True)  # New field

    def __str__(self):
        return f"{self.first_name} {self.last_name} - {self.email}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class DocumentType(models.Model):
    name = models.CharField(max_length=100,
                            unique=True, help_text="Name of the document type (e.g., Work Permit, Visa).")
    description = models.TextField(blank=True,
                                   help_text="Optional description of the document type.")

    def __str__(self):
        return self.name


class EmployeeDocument(models.Model):
    user = models.ForeignKey(CustomUser,
                             on_delete=models.CASCADE,
                             related_name="documents")
    document_type = models.ForeignKey(DocumentType, on_delete=models.CASCADE,
                                      related_name="documents")
    document_number = models.CharField(max_length=100,
                                       blank=True, null=True,
                                       help_text="Unique identifier, if applicable.")
    issue_date = models.DateField(blank=True,
                                  null=True,
                                  help_text="The date the document was issued.")
    expiration_date = models.DateField(blank=True,
                                       null=True,
                                       help_text="The date the document expires, if applicable.")
    notes = models.TextField(blank=True,
                             help_text="Additional notes about the document.")
    # file = models.FileField(upload_to="employee_documents/",
    # blank=True, null=True, help_text="Upload a copy of the document.")

    def is_expired(self):
        return self.expiration_date and self.expiration_date < now().date()

    def __str__(self):
        return f"{self.document_type.name} for {self.user.username}"


class EmployerSMSTask(models.Model):
    employer = models.ForeignKey(
        "Employer",
        on_delete=models.CASCADE,
        related_name="sms_tasks"
    )
    task_name = models.CharField(max_length=255)  # Stores task name
    is_enabled = models.BooleanField(default=True)  # Toggle SMS per employer

    class Meta:
        unique_together = ("employer", "task_name")  
        # Ensure one entry per task per employer

    def __str__(self):
        return f"{self.employer} - {self.task_name} - {'Enabled' if self.is_enabled else 'Disabled'}"


class PhoneEntry(models.Model):
    phone_number = PhoneNumberField(
        blank=False,
        null=False,
        help_text="Enter a phone number (e.g., 519-555-1234 or +15195551234)"
    )

    def __str__(self):
        return str(self.phone_number)


class EmployerSettings(models.Model):
    employer = models.OneToOneField(Employer,
                                    on_delete=models.CASCADE,
                                    related_name="settings")
    send_new_hire_file = models.BooleanField(default=True)  
    # ✅ Toggle for new hire file
    new_hire_invite_expiry_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Days a new-hire registration link stays valid. "
            "Leave blank to use the site default "
            "(settings.NEW_HIRE_INVITE_EXPIRY_DAYS). "
            "0 = never expire."
        ),
    )

    def __str__(self):
        return f"{self.employer.name} - {'Send' if self.send_new_hire_file else 'Do Not Send'} New Hire File"


def invite_expiry_days(employer=None):
    """Return TTL in days for a new-hire registration link (0 = never)."""
    default = int(getattr(settings, "NEW_HIRE_INVITE_EXPIRY_DAYS", 14) or 0)
    if employer is None:
        return default
    try:
        override = employer.settings.new_hire_invite_expiry_days
    except EmployerSettings.DoesNotExist:
        return default
    if override is None:
        return default
    return int(override)


def generate_random_token():
    """Generate a secure 64-character token."""
    return get_random_string(64)


class NewHireInvite(models.Model):
    employer = models.ForeignKey("user.Employer",
                                 on_delete=models.CASCADE,
                                 related_name="invites")
    invited_by = models.ForeignKey(   # NEW FIELD
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_invites",
        help_text="The user (manager/HR) who created this invite"
    )
    email = models.EmailField(unique=True)
    name = models.CharField(max_length=255)
    role = models.CharField(max_length=100,
                            choices=[("GSA", "GSA"), ("HR", "HR"),
                                     ("Manager", "Manager")])
    token = models.CharField(max_length=64,
                             unique=True, default=generate_random_token)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=(
            "When this registration link stops working. "
            "Blank on older invites means no time limit."
        ),
    )
    used = models.BooleanField(default=False)

    def get_invite_link(self):
        base_url = settings.SITE_URL  # Set this dynamically
        return f"{base_url}/register/{self.token}/"

    def set_expiry(self, days=None):
        """Set expires_at from employer/site TTL. days=0 clears expiry."""
        if days is None:
            days = invite_expiry_days(self.employer)
        days = int(days or 0)
        if days > 0:
            self.expires_at = now() + timedelta(days=days)
        else:
            self.expires_at = None
        return self.expires_at

    def refresh_expiry(self, save=True):
        self.set_expiry()
        if save and self.pk:
            self.save(update_fields=["expires_at"])
        return self.expires_at

    def is_expired(self):
        """Legacy rows with expires_at=NULL never time out."""
        if self.expires_at is None:
            return False
        return now() >= self.expires_at

    def save(self, *args, **kwargs):
        if not self.pk and self.expires_at is None:
            self.set_expiry()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Invite for {self.name} ({self.email})"
