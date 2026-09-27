import re

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML, Div, Field, Layout, Submit
from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import (
    MaxLengthValidator,
    MinLengthValidator,
    RegexValidator,
)
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.crypto import get_random_string
from django_countries import countries
from phonenumbers import PhoneNumberFormat, format_number, is_valid_number, parse
from phonenumbers.phonenumberutil import NumberParseException

from arl.utils.crypto import normalize_digits, sin_luhn_valid

from .models import CustomUser, Employer, NewHireInvite, Store
from arl.user.services import set_user_sin


def _reg_attrs(**extra):
    attrs = {"class": "form-control reg-input"}
    attrs.update(extra)
    return attrs


class CustomUserCreationForm(UserCreationForm):
    # WRITE-ONLY SIN FIELD (not bound to model)
    sin_input = forms.CharField(
        label="SIN (9 digits)",
        required=True,
        validators=[
            MinLengthValidator(9),
            MaxLengthValidator(9),
            RegexValidator(r"^\d{9}$", "SIN number must be 9 digits"),
        ],
        widget=forms.TextInput(attrs={"autocomplete": "off"}),
    )

    store = forms.ModelChoiceField(
        queryset=Store.objects.none(),
        required=False,  # ✅ Make store optional for employers
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    employer = forms.ModelChoiceField(
        queryset=Employer.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-control", "readonly": "readonly"}),
    )

    class Meta(UserCreationForm.Meta):
        model = CustomUser
        fields = (
            "employer",
            "store",
            "username",
            "password1",
            "password2",
            "first_name",
            "last_name",
            "dob",
            "email",
            "phone_number",
            "sin_input",
            "sin_expiration_date",
            "work_permit_expiration_date",
            "address",
            "address_two",
            "city",
            "state_province",
            "country",
            "postal",
        )
        widgets = {
            "dob": forms.DateInput(attrs={"type": "date"}),
            "sin_expiration_date": forms.DateInput(attrs={"type": "date"}),
            "work_permit_expiration_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        employer = kwargs.pop("employer", None)
        user_role = kwargs.pop("role", None)  # ✅ Get role from view or invite
        super().__init__(*args, **kwargs)

        # ✅ Set employer field
        if employer:
            self.fields["employer"].initial = employer
            stores = Store.objects.filter(employer=employer)

            if user_role == "EMPLOYER":
                # ✅ Employers do not need a store
                self.fields["store"].queryset = Store.objects.none()
                self.fields["store"].widget.attrs["disabled"] = (
                    "disabled"  # Prevent selection
                )
                self.fields["store"].empty_label = "No store (You can add one later)"
                self.fields["store"].required = False
            else:
                # ✅ Managers and GSAs should see employer's stores
                self.fields["store"].queryset = stores
                if not stores.exists():
                    self.fields["store"].empty_label = "No store yet (Check with admin)"
                self.fields["store"].required = True
        else:
            # If no employer is provided, the store list is empty
            self.fields["store"].queryset = Store.objects.none()

        # ✅ Ensure phone number retains input after a failed form submission
        phone_number_value = self.data.get("phone_number") or (
            self.instance.phone_number if self.instance else None
        )
        if phone_number_value:
            self.fields["phone_number"].initial = phone_number_value

        # ✅ Required fields
        required_fields = [
            "first_name",
            "last_name",
            "address",
            "city",
            "state_province",
            "country",
            "postal",
            "email",
            "sin_input",
            "dob",
        ]
        for field in required_fields:
            self.fields[field].required = True

        self.fields["address_two"].required = False  # Optional field

        # ✅ Consistent Styling
        for field_name in self.fields:
            self.fields[field_name].widget.attrs["class"] = "mt-1 mb-2 form-control"

        # ✅ Crispy Forms Setup
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.add_input(Submit("submit", "Register"))
        self.helper.form_action = reverse_lazy("index")

    def clean_phone_number(self):
        phone_number = self.cleaned_data.get("phone_number")
        if (
            phone_number
            and CustomUser.objects.filter(phone_number=phone_number).exists()
        ):
            raise forms.ValidationError("This phone number is already in use.")
        return phone_number

    def clean_email(self):
        email = self.cleaned_data["email"].lower()
        if CustomUser.objects.filter(email=email).exists():
            raise forms.ValidationError("This email is already in use.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        sin = cleaned_data.get("sin_input")
        sin_expiration_date = cleaned_data.get("sin_expiration_date")
        work_permit_expiration_date = cleaned_data.get("work_permit_expiration_date")

        if sin and sin.startswith("9"):
            if not sin_expiration_date:
                self.add_error(
                    "sin_expiration_date",
                    "SIN expiration date is required for SINs starting with 9.",
                )
            if not work_permit_expiration_date:
                self.add_error(
                    "work_permit_expiration_date",
                    "Work permit expiration date is required for SINs starting with 9.",
                )

        return cleaned_data

    def save(self, commit=True):
        """
        Create the user, then encrypt & store the SIN into
        sin_encrypted/sin_last4/sin_hash. Never save plaintext.
        """
        user = super().save(commit=False)

        # Ensure employer/store disabled field doesn't block save
        if self.fields["store"].widget.attrs.get("disabled"):
            user.store = None

        if commit:
            user.save()

        return user


class TwoFactorAuthenticationForm(forms.Form):
    verification_code = forms.CharField(
        max_length=12,
        required=True,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )

    def __init__(self, *args, **kwargs):
        super(TwoFactorAuthenticationForm, self).__init__(*args, **kwargs)
        self.fields["verification_code"].widget.attrs.update(
            {"style": "margin: 10px 0;"}
        )


class NewHireInviteForm(forms.ModelForm):
    """Form for inviting a new hire with a role selection."""

    # Optional inputs for who left
    departed_name = forms.CharField(
        required=False,
        label="Departing Employee Name",
        widget=forms.TextInput(
            attrs={
                "class": "form-control mb-2 placeholder-light",
                "placeholder": "If replacing someone, enter their name",
            }
        ),
    )
    departed_email = forms.EmailField(
        required=False,
        label="Departing Employee Email",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control mb-2 placeholder-light",
                "placeholder": "If replacing someone, enter their email",
            }
        ),
    )

    class Meta:
        model = NewHireInvite
        fields = ["email", "name", "role"]
        widgets = {
            "email": forms.EmailInput(
                attrs={
                    "class": "form-control placeholder-light",
                    "placeholder": "Enter new hire's email",
                }
            ),
            "name": forms.TextInput(
                attrs={
                    "class": "form-control placeholder-light",
                    "placeholder": "Full Name",
                }
            ),
            "role": forms.Select(attrs={"class": "form-control mb-4"}),
        }

    def __init__(self, *args, **kwargs):
        self.employer = kwargs.pop("employer", None)
        self.invited_by = kwargs.pop("invited_by", None)
        super().__init__(*args, **kwargs)

        # ✅ Dynamically set role choices
        self.fields["role"].choices = [
            ("Manager", "Manager"),
            ("GSA", "GSA"),
            ("CSR", "HR"),
        ]

        self.helper = FormHelper()
        self.helper.layout = Layout(
            "email",
            "name",
            "role",
            Div(
                HTML("<hr><h6 class='mt-3'>Optional: Who is this replacing?</h6>"),
                Field("departed_name", wrapper_class="mb-2"),
                Field("departed_email"),
                css_class="p-3 border rounded bg-light mt-3",
            ),
        )

    def clean_email(self):
        """Ensure the email isn't already invited and unused."""
        email = self.cleaned_data["email"].lower()
        if NewHireInvite.objects.filter(email=email, used=False).exists():
            raise forms.ValidationError("An invite for this email already exists.")
        return email

    def save(self, commit=True):
        """Ensure employer is assigned & token is generated if missing."""
        invite = super().save(commit=False)
        invite.employer = self.employer
        if self.invited_by:
            invite.invited_by = self.invited_by  # ✅ Assign employer automatically
        invite.token = invite.token or get_random_string(
            64
        )  # ✅ Generate token if missing

        if commit:
            invite.save()
        return invite


class PhoneStepForm(forms.Form):
    """Mobile number for the registration SMS code."""

    phone_number = forms.CharField(
        label="Mobile phone",
        max_length=20,
        widget=forms.TextInput(
            attrs=_reg_attrs(
                type="tel",
                inputmode="tel",
                autocomplete="tel",
                autocapitalize="off",
                placeholder="416 555 0100",
            )
        ),
        help_text="Include the area code. We'll text a code to this number.",
        error_messages={
            "required": "Enter the mobile number where we can text your code.",
        },
    )

    def clean_phone_number(self):
        raw = (self.cleaned_data.get("phone_number") or "").strip()
        try:
            parsed = parse(raw, "CA")
        except NumberParseException:
            raise forms.ValidationError(
                "Enter a valid phone number, including the area code."
            )
        if not is_valid_number(parsed):
            raise forms.ValidationError(
                "That phone number doesn't look valid. Check the digits and try again."
            )
        e164 = format_number(parsed, PhoneNumberFormat.E164)
        if CustomUser.objects.filter(phone_number=e164).exists():
            raise forms.ValidationError(
                "This phone number is already registered. Use a different number or contact HR."
            )
        return e164


class OTPForm(forms.Form):
    """Code from the SMS we just sent."""

    verification_code = forms.CharField(
        label="Text message code",
        max_length=12,
        widget=forms.TextInput(
            attrs=_reg_attrs(
                inputmode="numeric",
                autocomplete="one-time-code",
                autocapitalize="off",
                placeholder="123456",
            )
        ),
        help_text="Enter the code from the text message.",
        error_messages={
            "required": "Enter the code from the text message.",
        },
    )

    def clean_verification_code(self):
        digits = normalize_digits(self.cleaned_data.get("verification_code") or "")
        if len(digits) < 4:
            raise forms.ValidationError("Enter the numeric code from the text message.")
        return digits


class StoreChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        bits = [f"Store {obj.number}"]
        place = ", ".join(part for part in (obj.address, obj.city) if part)
        if place:
            bits.append(place)
        return " — ".join(bits)


class YouStepForm(forms.Form):
    """Identity, login, and SIN. The SIN never stays in plaintext."""

    username = forms.CharField(
        label="Username",
        max_length=150,
        validators=[UnicodeUsernameValidator()],
        widget=forms.TextInput(
            attrs=_reg_attrs(
                autocomplete="username", autocapitalize="off", spellcheck="false"
            )
        ),
        help_text="You'll use this to log in.",
        error_messages={
            "required": "Pick a username.",
            "max_length": "Keep the username to 150 characters or fewer.",
        },
    )
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs=_reg_attrs(autocomplete="new-password")),
        help_text="At least 8 characters. You'll use this to log in.",
        error_messages={"required": "Choose a password."},
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(attrs=_reg_attrs(autocomplete="new-password")),
        error_messages={"required": "Type the password again."},
    )
    first_name = forms.CharField(
        label="First name",
        max_length=150,
        widget=forms.TextInput(attrs=_reg_attrs(autocomplete="given-name")),
        error_messages={
            "required": "Enter your first name.",
            "max_length": "Keep your first name to 150 characters or fewer.",
        },
    )
    last_name = forms.CharField(
        label="Last name",
        max_length=150,
        widget=forms.TextInput(attrs=_reg_attrs(autocomplete="family-name")),
        error_messages={
            "required": "Enter your last name.",
            "max_length": "Keep your last name to 150 characters or fewer.",
        },
    )
    dob = forms.DateField(
        label="Date of birth",
        widget=forms.DateInput(attrs=_reg_attrs(type="date", autocomplete="bday")),
        error_messages={
            "required": "Enter your date of birth.",
            "invalid": "Enter a real date of birth.",
        },
    )
    address = forms.CharField(
        label="Street address",
        max_length=100,
        widget=forms.TextInput(attrs=_reg_attrs(autocomplete="address-line1")),
        error_messages={
            "required": "Enter your street address.",
            "max_length": "Keep the address to 100 characters or fewer.",
        },
    )
    address_two = forms.CharField(
        label="Apartment, unit, or suite",
        max_length=100,
        required=False,
        widget=forms.TextInput(attrs=_reg_attrs(autocomplete="address-line2")),
        error_messages={
            "max_length": "Keep the apartment or unit to 100 characters or fewer.",
        },
    )
    city = forms.CharField(
        label="City",
        max_length=100,
        widget=forms.TextInput(attrs=_reg_attrs(autocomplete="address-level2")),
        error_messages={
            "required": "Enter your city.",
            "max_length": "Keep the city to 100 characters or fewer.",
        },
    )
    state_province = forms.CharField(
        label="Province or state",
        max_length=100,
        widget=forms.TextInput(attrs=_reg_attrs(autocomplete="address-level1")),
        help_text="For example, ON.",
        error_messages={
            "required": "Enter your province or state.",
            "max_length": "Keep the province or state to 100 characters or fewer.",
        },
    )
    country = forms.ChoiceField(
        label="Country",
        choices=[("", "Select a country")] + list(countries),
        initial="CA",
        widget=forms.Select(attrs=_reg_attrs(autocomplete="country")),
        error_messages={
            "required": "Select your country.",
            "invalid_choice": "Select a country from the list.",
        },
    )
    postal = forms.CharField(
        label="Postal code",
        max_length=12,
        widget=forms.TextInput(
            attrs=_reg_attrs(autocomplete="postal-code", autocapitalize="characters")
        ),
        error_messages={"required": "Enter your postal code."},
    )
    TEMPORARY_SIN_EXPIRY_ERROR = "SINs that start with 9 need an expiry date."
    TEMPORARY_SIN_PERMIT_ERROR = (
        "SINs that start with 9 need a work permit expiry date."
    )

    sin_input = forms.CharField(
        label="Social Insurance Number (SIN)",
        max_length=20,
        widget=forms.TextInput(
            attrs=_reg_attrs(
                inputmode="numeric",
                autocomplete="off",
                autocapitalize="off",
                spellcheck="false",
                **{"data-temporary-sin-input": "true"},
            )
        ),
        help_text="9 digits. Spaces are okay. We encrypt this before saving it.",
        error_messages={"required": "Enter the 9 digits of your SIN. Spaces are okay."},
    )
    sin_expiration_date = forms.DateField(
        label="SIN expiry date",
        required=False,
        widget=forms.DateInput(
            attrs=_reg_attrs(type="date", **{"data-temporary-sin-date": "sin"})
        ),
        help_text="Required when your SIN starts with 9.",
        error_messages={"invalid": "Enter a real SIN expiry date."},
    )
    work_permit_expiration_date = forms.DateField(
        label="Work permit expiry date",
        required=False,
        widget=forms.DateInput(
            attrs=_reg_attrs(type="date", **{"data-temporary-sin-date": "permit"})
        ),
        help_text="Required when your SIN starts with 9.",
        error_messages={"invalid": "Enter a real work permit expiry date."},
    )

    def __init__(self, *args, invite_email="", **kwargs):
        self.invite_email = invite_email
        super().__init__(*args, **kwargs)
        today = timezone.localdate().isoformat()
        self.fields["dob"].widget.attrs["max"] = today
        self.fields["dob"].widget.attrs["min"] = "1900-01-01"
        self._mark_temporary_sin_dates()

    def _posted_sin_digits(self):
        if self.is_bound:
            raw = self.data.get(self.add_prefix("sin_input"), "")
        else:
            raw = (self.initial or {}).get("sin_input", "")
        return normalize_digits(str(raw or ""))

    def _mark_temporary_sin_dates(self):
        """HTML required state only. Server messages stay on the date fields."""
        temporary = self._posted_sin_digits().startswith("9")
        for name in ("sin_expiration_date", "work_permit_expiration_date"):
            attrs = self.fields[name].widget.attrs
            if temporary:
                attrs["required"] = True
                attrs["aria-required"] = "true"
            else:
                attrs.pop("required", None)
                attrs.pop("aria-required", None)

    def clean_username(self):
        username = (self.cleaned_data.get("username") or "").strip()
        if CustomUser.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("That username is already taken. Pick another.")
        return username

    def clean_sin_input(self):
        digits = normalize_digits(self.cleaned_data.get("sin_input") or "")
        if len(digits) != 9:
            raise forms.ValidationError(
                "Enter the 9 digits of your SIN. Spaces are okay."
            )
        if not sin_luhn_valid(digits):
            raise forms.ValidationError(
                "Those digits aren't a valid SIN. Check for a typo."
            )
        return digits

    def clean(self):
        cleaned = super().clean()
        self._clean_passwords(cleaned)
        self._clean_dob(cleaned)
        self._clean_postal(cleaned)
        self._clean_sin_dates(cleaned)
        return cleaned

    def _clean_passwords(self, cleaned):
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 and password2 and password1 != password2:
            self.add_error(
                "password2", "The two passwords don't match. Type them again."
            )
        if password1 and not self.errors.get("password1"):
            user = CustomUser(
                username=cleaned.get("username") or "",
                first_name=cleaned.get("first_name") or "",
                last_name=cleaned.get("last_name") or "",
                email=self.invite_email or "",
            )
            try:
                password_validation.validate_password(password1, user=user)
            except DjangoValidationError as exc:
                self.add_error("password1", exc)

    def _clean_dob(self, cleaned):
        dob = cleaned.get("dob")
        if not dob:
            return
        today = timezone.localdate()
        if dob > today:
            self.add_error("dob", "Date of birth has to be before today.")
        elif dob.year < 1900:
            self.add_error("dob", "Check the year on your date of birth.")

    def _clean_postal(self, cleaned):
        postal = cleaned.get("postal")
        if not postal:
            return
        if cleaned.get("country") == "CA":
            compact = re.sub(r"[^A-Za-z0-9]", "", postal).upper()
            if not re.fullmatch(r"[A-Z]\d[A-Z]\d[A-Z]\d", compact):
                self.add_error("postal", "Enter a Canadian postal code, like A1A 1A1.")
                return
            cleaned["postal"] = f"{compact[:3]} {compact[3:]}"
            return
        compact = postal.strip()
        if len(compact) > 7:
            self.add_error("postal", "Enter a postal code of 7 characters or fewer.")
            return
        cleaned["postal"] = compact

    def _clean_sin_dates(self, cleaned):
        # Digits only, so "900-000-001" and "9 00000001" follow the same rule.
        # Use the posted value when the SIN field itself failed validation,
        # so a number that starts with 9 still asks for both dates.
        digits = cleaned.get("sin_input") or self._posted_sin_digits()
        if not str(digits).startswith("9"):
            return
        if not cleaned.get("sin_expiration_date"):
            self.add_error(
                "sin_expiration_date",
                self.TEMPORARY_SIN_EXPIRY_ERROR,
            )
        if not cleaned.get("work_permit_expiration_date"):
            self.add_error(
                "work_permit_expiration_date",
                self.TEMPORARY_SIN_PERMIT_ERROR,
            )


class WorkStepForm(forms.Form):
    """Store selection. Employer and role come from the invite."""

    store = StoreChoiceField(
        label="Store",
        queryset=Store.objects.none(),
        required=False,
        empty_label="Select your store",
        widget=forms.Select(attrs=_reg_attrs()),
        error_messages={
            "invalid_choice": "Choose a store from the list.",
        },
    )

    def __init__(self, *args, employer, role, **kwargs):
        self.role = role
        super().__init__(*args, **kwargs)
        self.fields["store"].queryset = Store.objects.filter(
            employer=employer, is_active=True
        ).order_by("number")

    def clean_store(self):
        store = self.cleaned_data.get("store")
        if self.role == "EMPLOYER":
            return None
        if store is None:
            if not self.fields["store"].queryset.exists():
                raise forms.ValidationError(
                    "This employer has no active stores yet. Ask HR to add a store, then come back to this invite."
                )
            raise forms.ValidationError("Choose the store you will work at.")
        return store
