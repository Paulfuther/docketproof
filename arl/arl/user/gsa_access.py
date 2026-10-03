from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse_lazy

SESSION_GSA_PREVIEW_USER_ID = "gsa_preview_user_id"


def is_gsa_account(user):
    return bool(
        user.is_authenticated and getattr(user, "is_employee_account", False)
    )


def can_admin_gsa_preview(user):
    return bool(
        user.is_authenticated and (user.is_staff or user.is_superuser)
    )


def gsa_users_for_admin_preview(admin_user):
    """GSAs the admin may preview: same company, or all GSAs for superusers."""
    User = get_user_model()
    qs = (
        User.objects.filter(groups__name="GSA", is_active=True)
        .select_related("employer")
        .distinct()
    )
    if not admin_user.is_superuser and admin_user.employer_id:
        qs = qs.filter(employer_id=admin_user.employer_id)
    return qs.order_by("first_name", "last_name", "username")


def _preview_user_allowed(admin_user, gsa_user):
    if not can_admin_gsa_preview(admin_user):
        return False
    if not gsa_user.is_employee_account:
        return False
    if admin_user.is_superuser:
        return True
    if admin_user.employer_id and gsa_user.employer_id == admin_user.employer_id:
        return True
    return False


def get_gsa_preview_user(request):
    if not can_admin_gsa_preview(request.user):
        return None
    user_id = request.session.get(SESSION_GSA_PREVIEW_USER_ID)
    if not user_id:
        return None
    User = get_user_model()
    try:
        gsa_user = User.objects.select_related("employer").get(pk=user_id)
    except User.DoesNotExist:
        return None
    if not _preview_user_allowed(request.user, gsa_user):
        return None
    return gsa_user


def is_gsa_preview_active(request):
    return get_gsa_preview_user(request) is not None


def shows_gsa_chrome(request):
    if is_gsa_account(request.user):
        return True
    return is_gsa_preview_active(request)


def gsa_actor(request):
    """User whose GSA data should be shown (preview subject or logged-in GSA)."""
    preview_user = get_gsa_preview_user(request)
    if preview_user:
        return preview_user
    if is_gsa_account(request.user):
        return request.user
    return None


def set_gsa_preview(request, gsa_user):
    if not _preview_user_allowed(request.user, gsa_user):
        return False
    request.session[SESSION_GSA_PREVIEW_USER_ID] = gsa_user.pk
    return True


def clear_gsa_preview(request):
    request.session.pop(SESSION_GSA_PREVIEW_USER_ID, None)


def gsa_preview_blocks_mutation(request):
    """Block writes while an admin is previewing a GSA account."""
    if is_gsa_preview_active(request):
        messages.error(request, "GSA preview is read-only.")
        return redirect("employee_home")
    return None


class GSAOrPermissionRequiredMixin(PermissionRequiredMixin):
    """GSA accounts with an employer may use the same forms as permitted staff."""

    def has_permission(self):
        user = self.request.user
        if is_gsa_account(user) and user.employer_id:
            return True
        return super().has_permission()


def post_form_success_url(user, request=None):
    if request is not None and is_gsa_preview_active(request):
        return reverse_lazy("employee_home")
    if is_gsa_account(user):
        return reverse_lazy("employee_home")
    return reverse_lazy("home")
