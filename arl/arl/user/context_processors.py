from arl.user.gsa_access import (
    get_gsa_preview_user,
    gsa_actor,
    is_gsa_account,
    shows_gsa_chrome,
)


def gsa_preview(request):
    preview_user = None
    chrome = False
    subject_user = None
    if request.user.is_authenticated:
        preview_user = get_gsa_preview_user(request)
        chrome = shows_gsa_chrome(request)
        if chrome:
            subject_user = gsa_actor(request) or (
                request.user if is_gsa_account(request.user) else None
            )
    return {
        "shows_gsa_chrome": chrome,
        "gsa_preview_active": preview_user is not None,
        "gsa_preview_user": preview_user,
        "gsa_subject_user": subject_user,
    }
