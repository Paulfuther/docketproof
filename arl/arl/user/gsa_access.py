from django.contrib.auth.mixins import PermissionRequiredMixin
from django.urls import reverse_lazy


def is_gsa_account(user):
    return bool(
        user.is_authenticated and getattr(user, "is_employee_account", False)
    )


class GSAOrPermissionRequiredMixin(PermissionRequiredMixin):
    """GSA accounts with an employer may use the same forms as permitted staff."""

    def has_permission(self):
        user = self.request.user
        if is_gsa_account(user) and user.employer_id:
            return True
        return super().has_permission()


def post_form_success_url(user):
    if is_gsa_account(user):
        return reverse_lazy("employee_home")
    return reverse_lazy("home")
