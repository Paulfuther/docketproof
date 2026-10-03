from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.shortcuts import render

from arl.dsign.models import SignedDocumentFile
from arl.user.employee_views import (
    _immigration_context,
    _pill_for_envelope,
    _unsigned_queryset,
)
from arl.user.models import CustomUser


def _forbid_unless_hr(user):
    if not getattr(user, "is_hr_account", False):
        return HttpResponseForbidden("You cannot view employees.")
    return None


def _company_employees(hr_user):
    return CustomUser.objects.filter(
        employer_id=hr_user.employer_id,
        is_active=True,
    ).order_by("last_name", "first_name", "username")


@login_required
def hr_employee_list(request):
    denied = _forbid_unless_hr(request.user)
    if denied:
        return denied
    return render(
        request,
        "user/hr/employee_list.html",
        {"employees": _company_employees(request.user)},
    )


@login_required
def hr_employee_detail(request, user_id):
    denied = _forbid_unless_hr(request.user)
    if denied:
        return denied
    employee = CustomUser.objects.filter(pk=user_id, is_active=True).first()
    if employee is None:
        return HttpResponseNotFound("Employee not found.")
    if employee.employer_id != request.user.employer_id:
        return HttpResponseForbidden("You cannot view this employee.")

    signed_documents = SignedDocumentFile.objects.filter(
        user=employee,
        employer_id=request.user.employer_id,
        is_company_document=False,
    ).order_by("-uploaded_at")
    unsigned_pills = [
        _pill_for_envelope(envelope) for envelope in _unsigned_queryset(employee)
    ]
    context = _immigration_context(employee)
    context.update(
        {
            "employee": employee,
            "signed_documents": signed_documents,
            "unsigned_pills": unsigned_pills,
        }
    )
    return render(request, "user/hr/employee_detail.html", context)
