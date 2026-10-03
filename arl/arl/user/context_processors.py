from arl.user.gsa_access import get_gsa_preview_user, shows_gsa_chrome


def gsa_preview(request):
    preview_user = None
    chrome = False
    if request.user.is_authenticated:
        preview_user = get_gsa_preview_user(request)
        chrome = shows_gsa_chrome(request)
    return {
        "shows_gsa_chrome": chrome,
        "gsa_preview_active": preview_user is not None,
        "gsa_preview_user": preview_user,
    }
