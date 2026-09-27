from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden
from django.shortcuts import render

from .services import build_document_audit, sort_param_or_date_hired


def _can_view_hr_documents(user):
    employer = getattr(user, "employer", None)
    if not employer:
        return False
    return user.groups.filter(name__in=["Manager", "EMPLOYER"]).exists()


@login_required
def document_audit_log_partial(request):
    if not _can_view_hr_documents(request.user):
        return HttpResponseForbidden("Not allowed.")

    context = build_document_audit(
        employer=request.user.employer,
        search_query=request.GET.get("audit_q", ""),
        incomplete_only=request.GET.get("audit_incomplete") == "1",
        sort=sort_param_or_date_hired(request.GET, "audit_sort"),
    )
    return render(
        request,
        "documentflow/partials/document_audit_log.html",
        context,
    )
