"""Multi-step new-hire registration.

Phone (SMS code) -> You (identity and encrypted SIN) -> Work -> Confirm.

The SIN is encrypted with the shared helpers before it touches the session
or the user row. Registration does not write the legacy plaintext ``sin`` column.
"""

import logging
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.hashers import make_password
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django_countries import countries
from phonenumbers import PhoneNumberFormat, format_number, parse
from phonenumbers.phonenumberutil import NumberParseException

from arl.user.forms import OTPForm, PhoneStepForm, WorkStepForm, YouStepForm
from arl.user.models import CustomUser, NewHireInvite, Store
from arl.user.services import set_user_sin
from arl.user.tasks import save_user_to_db
from arl.msg.helpers import check_verification_token, request_verification_token
from arl.utils.crypto import sin_decrypt, sin_encrypt, sin_last4, sin_luhn_valid

logger = logging.getLogger(__name__)

SESSION_KEY = "newhire_registration"
REGISTERED_EMAIL_KEY = "newhire_registered_email"
CODE_RESEND_SECONDS = 30

STEPS = (
    ("phone", "Phone"),
    ("you", "You"),
    ("work", "Work"),
    ("confirm", "Confirm"),
)
STEP_INDEX = {key: index for index, (key, _label) in enumerate(STEPS)}
STEP_TITLES = {
    "phone": "Verify your phone",
    "you": "About you",
    "work": "Where you'll work",
    "confirm": "Confirm and submit",
}
ROLE_LABELS = {
    "GSA": "GSA",
    "HR": "HR",
    "CSR": "HR",
    "Manager": "Manager",
    "EMPLOYER": "Employer",
}


class RegistrationError(Exception):
    """A problem we can show on the confirm step without saving."""


def empty_draft(token):
    return {
        "token": token,
        "phone_e164": "",
        "phone_verified": False,
        "code_sent": False,
        "code_sent_at": "",
        "you_complete": False,
        "work_complete": False,
        "you": {},
        "work": {},
    }


def get_draft(request, token):
    bucket = request.session.get(SESSION_KEY)
    if not isinstance(bucket, dict):
        bucket = {}
    draft = bucket.get(token)
    if not isinstance(draft, dict) or draft.get("token") != token:
        draft = empty_draft(token)
        bucket[token] = draft
        request.session[SESSION_KEY] = bucket
        request.session.modified = True
    return draft


def save_draft(request, draft):
    bucket = request.session.get(SESSION_KEY)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[draft["token"]] = draft
    request.session[SESSION_KEY] = bucket
    request.session.modified = True


def clear_draft(request, token):
    bucket = request.session.get(SESSION_KEY)
    if isinstance(bucket, dict) and token in bucket:
        bucket.pop(token, None)
        request.session[SESSION_KEY] = bucket
        request.session.modified = True


def furthest_step(draft):
    if not draft.get("phone_verified"):
        return "phone"
    if not draft.get("you_complete"):
        return "you"
    if not draft.get("work_complete"):
        return "work"
    return "confirm"


def step_allowed(draft, step):
    if step not in STEP_INDEX:
        return False
    return STEP_INDEX[step] <= STEP_INDEX[furthest_step(draft)]


def step_url(token, step):
    return f"{reverse('register', args=[token])}?step={step}"


def previous_step(step):
    index = STEP_INDEX.get(step, 0)
    return STEPS[max(0, index - 1)][0]


def role_label(role):
    return ROLE_LABELS.get(role, role or "")


def display_phone(e164):
    if not e164:
        return ""
    try:
        return format_number(parse(e164, None), PhoneNumberFormat.NATIONAL)
    except NumberParseException:
        return e164


def split_invite_name(name):
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def parse_sent_at(value):
    if not value:
        return None
    try:
        sent = datetime.fromisoformat(value)
    except ValueError:
        return None
    if timezone.is_naive(sent):
        sent = timezone.make_aware(sent, timezone.utc)
    return sent


def too_soon(draft):
    sent = parse_sent_at(draft.get("code_sent_at"))
    if sent is None:
        return False
    return timezone.now() - sent < timedelta(seconds=CODE_RESEND_SECONDS)


def you_initial(draft, invite):
    you = draft.get("you") or {}
    first, last = split_invite_name(invite.name)
    return {
        "first_name": you.get("first_name") or first,
        "last_name": you.get("last_name") or last,
        "username": you.get("username") or "",
        "dob": you.get("dob") or "",
        "address": you.get("address") or "",
        "address_two": you.get("address_two") or "",
        "city": you.get("city") or "",
        "state_province": you.get("state_province") or "",
        "country": you.get("country") or "CA",
        "postal": you.get("postal") or "",
        "sin_expiration_date": you.get("sin_expiration_date") or "",
        "work_permit_expiration_date": you.get("work_permit_expiration_date") or "",
    }


def pack_you(cleaned):
    """Session payload. SIN is ciphertext only; password is a hash."""
    digits = cleaned["sin_input"]
    sin_exp = cleaned.get("sin_expiration_date")
    permit_exp = cleaned.get("work_permit_expiration_date")
    return {
        "username": cleaned["username"],
        "password_hash": make_password(cleaned["password1"]),
        "first_name": cleaned["first_name"],
        "last_name": cleaned["last_name"],
        "dob": cleaned["dob"].isoformat(),
        "address": cleaned["address"],
        "address_two": cleaned.get("address_two") or "",
        "city": cleaned["city"],
        "state_province": cleaned["state_province"],
        "country": cleaned["country"],
        "postal": cleaned["postal"],
        "sin_encrypted": sin_encrypt(digits),
        "sin_last4": sin_last4(digits),
        "temporary_sin": digits.startswith("9"),
        "sin_expiration_date": sin_exp.isoformat() if sin_exp else "",
        "work_permit_expiration_date": permit_exp.isoformat() if permit_exp else "",
    }


def parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def mark_invalid(form):
    if form is None or not getattr(form, "is_bound", False):
        return form
    for name in form.errors:
        field = form.fields.get(name)
        if field is None:
            continue
        css = field.widget.attrs.get("class", "")
        if "is-invalid" not in css.split():
            field.widget.attrs["class"] = f"{css} is-invalid".strip()
    return form


def step_nav(token, draft, current):
    furthest = STEP_INDEX[furthest_step(draft)]
    items = []
    for index, (key, label) in enumerate(STEPS):
        if key == current:
            state = "current"
        elif index < furthest:
            state = "done"
        elif index == furthest:
            state = "available"
        else:
            state = "upcoming"
        items.append(
            {
                "key": key,
                "label": label,
                "number": index + 1,
                "state": state,
                "url": step_url(token, key) if state in {"done", "available"} else "",
            }
        )
    return items


def load_invite(request, token):
    invite = (
        NewHireInvite.objects.filter(token=token).select_related("employer").first()
    )
    if invite is None:
        raise Http404("Invite not found.")
    if invite.used:
        return None, render(
            request,
            "user/invite_used.html",
            {"invite": invite},
        )
    if invite.is_expired():
        return None, render(
            request,
            "user/invite_expired.html",
            {"invite": invite, "expires_at": invite.expires_at},
        )
    return invite, None


def build_review(draft, invite):
    you = draft.get("you") or {}
    work = draft.get("work") or {}
    store = None
    store_id = work.get("store_id")
    if store_id:
        store = Store.objects.filter(pk=store_id, employer=invite.employer).first()
    country_code = you.get("country") or ""
    needs_store = invite.role != "EMPLOYER"
    return {
        "phone": display_phone(draft.get("phone_e164")),
        "email": invite.email,
        "role": role_label(invite.role),
        "employer": invite.employer.name if invite.employer else "",
        "store": store,
        "needs_store": needs_store,
        "store_missing": needs_store and store is None,
        "first_name": you.get("first_name") or "",
        "last_name": you.get("last_name") or "",
        "username": you.get("username") or "",
        "dob": you.get("dob") or "",
        "address": you.get("address") or "",
        "address_two": you.get("address_two") or "",
        "city": you.get("city") or "",
        "state_province": you.get("state_province") or "",
        "country": dict(countries).get(country_code, country_code),
        "postal": you.get("postal") or "",
        "sin_mask": f"*** *** {you['sin_last4']}" if you.get("sin_last4") else "",
        "temporary_sin": bool(you.get("temporary_sin")),
        "sin_expiration_date": you.get("sin_expiration_date") or "",
        "work_permit_expiration_date": you.get("work_permit_expiration_date") or "",
    }


def queue_follow_up(user, you):
    """Re-apply dates the way the previous registration task did.

    The payload has no SIN and no password.
    """
    payload = {
        "user_id": user.id,
        "employer": user.employer_id,
        "dob": you.get("dob"),
        "sin_expiration_date": you.get("sin_expiration_date") or None,
        "work_permit_expiration_date": you.get("work_permit_expiration_date") or None,
    }
    try:
        save_user_to_db.delay(**payload)
    except Exception:
        logger.exception(
            "Post-registration save task was not queued for user %s", user.id
        )


def create_new_hire(draft, invite):
    if not draft.get("phone_verified") or not draft.get("phone_e164"):
        raise RegistrationError("Verify your phone before submitting.")
    you = draft.get("you") or {}
    if not draft.get("you_complete") or not you.get("sin_encrypted"):
        raise RegistrationError("Go back to You and enter your details again.")
    if not draft.get("work_complete"):
        raise RegistrationError("Choose your store before submitting.")

    try:
        sin_plain = sin_decrypt(you.get("sin_encrypted"))
    except ValueError:
        sin_plain = None
    if not sin_plain or not sin_luhn_valid(sin_plain):
        raise RegistrationError(
            "We couldn't read your SIN. Go back to You and enter it again."
        )
    if sin_plain.startswith("9") and (
        not you.get("sin_expiration_date") or not you.get("work_permit_expiration_date")
    ):
        raise RegistrationError(
            "Go back to You and add the SIN and work permit expiry dates."
        )

    phone = draft["phone_e164"]
    if CustomUser.objects.filter(phone_number=phone).exists():
        raise RegistrationError(
            "That phone number was just registered. "
            "Go back to Phone and use a different number."
        )
    if CustomUser.objects.filter(email__iexact=invite.email).exists():
        raise RegistrationError(
            "An account with this email already exists. Ask HR for help."
        )
    if CustomUser.objects.filter(username__iexact=you.get("username") or "").exists():
        raise RegistrationError(
            "That username was just taken. Go back to You and pick another."
        )

    store = None
    if invite.role != "EMPLOYER":
        store = Store.objects.filter(
            pk=(draft.get("work") or {}).get("store_id"),
            employer=invite.employer,
            is_active=True,
        ).first()
        if store is None:
            raise RegistrationError(
                "Choose an active store on the Work step, then submit again."
            )

    user = CustomUser(
        username=you["username"],
        email=invite.email,
        first_name=you.get("first_name") or "",
        last_name=you.get("last_name") or "",
        dob=parse_date(you.get("dob")),
        phone_number=phone,
        address=you.get("address") or "",
        address_two=you.get("address_two") or "",
        city=you.get("city") or "",
        state_province=you.get("state_province") or "",
        country=you.get("country") or "",
        postal=you.get("postal") or "",
        employer=invite.employer,
        store=store,
        sin_expiration_date=parse_date(you.get("sin_expiration_date")),
        work_permit_expiration_date=parse_date(you.get("work_permit_expiration_date")),
    )
    user.password = you["password_hash"]
    # Encrypted fields only. Do not assign the legacy plaintext sin column.
    set_user_sin(user, sin_plain, validate_luhn=True, save=False)
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError:
        logger.exception("New hire registration conflict for invite %s", invite.pk)
        raise RegistrationError(
            "That username or email was just taken. Go back and change it."
        )
    return user


class RegisterView(View):
    def dispatch(self, request, *args, **kwargs):
        self.token = kwargs.get("token")
        invite, response = load_invite(request, self.token)
        if response is not None:
            return response
        self.invite = invite
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        draft = get_draft(request, self.token)
        requested = request.GET.get("step") or furthest_step(draft)
        if requested not in STEP_INDEX or not step_allowed(draft, requested):
            messages.error(request, "Finish the earlier steps first.")
            return redirect(step_url(self.token, furthest_step(draft)))
        return self.render_step(request, draft, requested)

    def post(self, request, *args, **kwargs):
        draft = get_draft(request, self.token)
        step = request.POST.get("step") or "phone"
        action = request.POST.get("action") or ""
        if action == "back":
            current = step if step in STEP_INDEX else furthest_step(draft)
            return redirect(step_url(self.token, previous_step(current)))
        if step not in STEP_INDEX or not step_allowed(draft, step):
            messages.error(request, "Finish the earlier steps first.")
            return redirect(step_url(self.token, furthest_step(draft)))
        handler = {
            "phone": self.post_phone,
            "you": self.post_you,
            "work": self.post_work,
            "confirm": self.post_confirm,
        }[step]
        return handler(request, draft, action)

    def post_phone(self, request, draft, action):
        if action == "change_number":
            draft["phone_verified"] = False
            draft["code_sent"] = False
            draft["code_sent_at"] = ""
            save_draft(request, draft)
            return redirect(step_url(self.token, "phone"))
        if action == "continue":
            if not draft.get("phone_verified"):
                messages.error(request, "Verify your phone before continuing.")
                return redirect(step_url(self.token, "phone"))
            return redirect(step_url(self.token, "you"))
        if action == "verify_code":
            return self._verify_code(request, draft)
        if action == "send_code":
            return self._handle_send_code(request, draft)
        messages.error(request, "Use the buttons on this page to continue.")
        return redirect(step_url(self.token, "phone"))

    def _handle_send_code(self, request, draft):
        raw = (request.POST.get("phone_number") or "").strip()
        if raw:
            form = PhoneStepForm(request.POST)
            if not form.is_valid():
                return self.render_step(
                    request, draft, "phone", form=form, phone_mode="enter"
                )
            return self._send_code(
                request,
                draft,
                form.cleaned_data["phone_number"],
                show_code_screen=False,
            )
        if draft.get("phone_e164"):
            return self._send_code(
                request,
                draft,
                draft["phone_e164"],
                show_code_screen=True,
            )
        form = PhoneStepForm(request.POST)
        form.is_valid()
        return self.render_step(request, draft, "phone", form=form, phone_mode="enter")

    def _send_code(self, request, draft, e164, show_code_screen):
        same_number = e164 == draft.get("phone_e164")
        if same_number and draft.get("code_sent") and too_soon(draft):
            messages.error(request, "Wait a few seconds before sending another code.")
            form = (
                OTPForm()
                if show_code_screen
                else PhoneStepForm(initial={"phone_number": e164})
            )
            mode = "code" if show_code_screen else "enter"
            return self.render_step(request, draft, "phone", form=form, phone_mode=mode)
        try:
            request_verification_token(e164)
        except Exception:
            logger.exception("Failed to send registration code")
            message = (
                "We couldn't send a text to that number. "
                "Check the number or try again in a minute."
            )
            if show_code_screen:
                form = OTPForm()
                form.add_error(None, message)
                mode = "code"
            else:
                form = PhoneStepForm(data={"phone_number": e164})
                form.is_valid()
                form.add_error(None, message)
                mode = "enter"
            return self.render_step(request, draft, "phone", form=form, phone_mode=mode)
        draft["phone_e164"] = e164
        draft["phone_verified"] = False
        draft["code_sent"] = True
        draft["code_sent_at"] = timezone.now().isoformat()
        save_draft(request, draft)
        return redirect(step_url(self.token, "phone"))

    def _verify_code(self, request, draft):
        form = OTPForm(request.POST)
        if not draft.get("code_sent") or not draft.get("phone_e164"):
            messages.error(request, "Request a code before entering it.")
            return redirect(step_url(self.token, "phone"))
        if not form.is_valid():
            return self.render_step(
                request, draft, "phone", form=form, phone_mode="code"
            )
        try:
            approved = bool(
                check_verification_token(
                    draft["phone_e164"],
                    form.cleaned_data["verification_code"],
                )
            )
        except Exception:
            logger.exception("Failed to check registration code")
            approved = False
        if not approved:
            form.add_error(
                "verification_code",
                "That code doesn't match. Check the text and try again, "
                "or send a new code.",
            )
            return self.render_step(
                request, draft, "phone", form=form, phone_mode="code"
            )
        draft["phone_verified"] = True
        save_draft(request, draft)
        messages.success(request, "Phone verified.")
        return redirect(step_url(self.token, "you"))

    def post_you(self, request, draft, action):
        form = YouStepForm(request.POST, invite_email=self.invite.email)
        valid = form.is_valid()
        if CustomUser.objects.filter(email__iexact=self.invite.email).exists():
            form.add_error(
                None,
                "An account with this email already exists. Ask HR for help.",
            )
            valid = False
        if valid:
            draft["you"] = pack_you(form.cleaned_data)
            draft["you_complete"] = True
            save_draft(request, draft)
            return redirect(step_url(self.token, "work"))
        draft["you_complete"] = False
        save_draft(request, draft)
        return self.render_step(request, draft, "you", form=form)

    def post_work(self, request, draft, action):
        form = WorkStepForm(
            request.POST,
            employer=self.invite.employer,
            role=self.invite.role,
        )
        if form.is_valid():
            store = form.cleaned_data.get("store")
            draft["work"] = {"store_id": store.pk if store else None}
            draft["work_complete"] = True
            save_draft(request, draft)
            return redirect(step_url(self.token, "confirm"))
        draft["work_complete"] = False
        save_draft(request, draft)
        return self.render_step(request, draft, "work", form=form)

    def post_confirm(self, request, draft, action):
        if action != "submit":
            return self.render_step(request, draft, "confirm")
        try:
            user = create_new_hire(draft, self.invite)
        except RegistrationError as exc:
            messages.error(request, str(exc))
            return self.render_step(request, draft, "confirm")
        except Exception:
            logger.exception(
                "New hire registration failed for invite %s", self.invite.pk
            )
            messages.error(
                request,
                "We couldn't save your registration. "
                "Your answers are still here — please try again.",
            )
            return self.render_step(request, draft, "confirm")
        queue_follow_up(user, draft.get("you") or {})
        clear_draft(request, self.token)
        request.session[REGISTERED_EMAIL_KEY] = self.invite.email
        request.session.modified = True
        messages.success(
            request,
            "Thank you for registering. "
            "Check your email for next steps from your employer.",
        )
        return redirect("register_complete")

    def render_step(self, request, draft, step, form=None, phone_mode=None):
        invite = self.invite
        if step == "phone":
            phone_mode, form = self._phone_form(draft, form, phone_mode)
        elif step == "you" and form is None:
            form = YouStepForm(
                initial=you_initial(draft, invite),
                invite_email=invite.email,
            )
        elif step == "work" and form is None:
            store_id = (draft.get("work") or {}).get("store_id")
            initial = {"store": store_id} if store_id else None
            form = WorkStepForm(
                initial=initial,
                employer=invite.employer,
                role=invite.role,
            )
        if form is not None:
            mark_invalid(form)
        review = build_review(draft, invite) if step == "confirm" else None
        return render(
            request,
            "user/register.html",
            {
                "invite": invite,
                "step": step,
                "step_title": STEP_TITLES[step],
                "steps": step_nav(self.token, draft, step),
                "form": form,
                "phone_mode": phone_mode,
                "phone_display": display_phone(draft.get("phone_e164")),
                "phone_verified": bool(draft.get("phone_verified")),
                "role_label": role_label(invite.role),
                "role_needs_store": invite.role != "EMPLOYER",
                "stores_available": Store.objects.filter(
                    employer=invite.employer, is_active=True
                ).exists(),
                "review": review,
                "email": invite.email,
            },
        )

    def _phone_form(self, draft, form, phone_mode):
        if phone_mode is None:
            if draft.get("phone_verified"):
                phone_mode = "verified"
            elif draft.get("code_sent"):
                phone_mode = "code"
            else:
                phone_mode = "enter"
        if form is None and phone_mode == "code":
            form = OTPForm()
        elif form is None and phone_mode == "enter":
            initial = {}
            if draft.get("phone_e164"):
                initial["phone_number"] = draft["phone_e164"]
            form = PhoneStepForm(initial=initial)
        elif phone_mode == "verified":
            form = None
        return phone_mode, form


def register_complete(request):
    email = request.session.get(REGISTERED_EMAIL_KEY)
    if not email:
        return redirect("home")
    return render(
        request,
        "user/register_complete.html",
        {"email": email},
    )
