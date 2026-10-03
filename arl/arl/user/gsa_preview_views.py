from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from arl.user.gsa_access import (
    can_admin_gsa_preview,
    clear_gsa_preview,
    gsa_users_for_admin_preview,
    set_gsa_preview,
)


@login_required
def gsa_preview_select(request):
    if not can_admin_gsa_preview(request.user):
        return redirect("home")

    gsa_users = gsa_users_for_admin_preview(request.user)
    if request.method == "POST":
        gsa_id = request.POST.get("gsa_user_id")
        if not gsa_id:
            messages.error(request, "Choose a GSA to preview.")
        else:
            selected = gsa_users.filter(pk=gsa_id).first()
            if selected is None:
                messages.error(request, "That GSA is not available for preview.")
            elif set_gsa_preview(request, selected):
                return redirect("employee_home")
            else:
                messages.error(request, "That GSA is not available for preview.")

    return render(
        request,
        "user/gsa_preview_select.html",
        {"gsa_users": gsa_users},
    )


@login_required
def gsa_preview_exit(request):
    clear_gsa_preview(request)
    messages.info(request, "GSA preview ended.")
    return redirect("home")
