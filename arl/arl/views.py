from django.shortcuts import render
import logging

logger = logging.getLogger(__name__)


def error_400(request, exception):
    logger.warning(
        "HTTP 400 on %s %s content_length=%s: %s",
        request.method,
        request.path,
        request.META.get("CONTENT_LENGTH"),
        exception,
    )
    return render(request, "incident/400.html", status=400)


def error_403(request, exception):
    data = {}
    return render(request, 'incident/403.html', data)


def error_500(request, exception=None):
    data = {}
    return render(request, 'incident/500.html', data)


def custom_405(request, exception):
    """Return a custom response for 405 Method Not Allowed errors."""
    return render(request, 'incident/405.html', status=405)


