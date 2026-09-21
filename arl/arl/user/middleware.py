import traceback

from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.db import connection
from django.http import Http404, HttpResponse

from .models import ErrorLog


class ErrorLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        return response

    def process_exception(self, request, exception):
        # Let Django turn these into 400/403/404. Swallowing
        # TooManyFieldsSent / RequestDataTooBig here turned the mobile
        # checklist submit into a generic 500 instead of HTTP 400.
        if isinstance(exception, (Http404, PermissionDenied, SuspiciousOperation)):
            if isinstance(exception, SuspiciousOperation):
                try:
                    ErrorLog.objects.create(
                        path=request.path,
                        method=request.method,
                        status_code=400,
                        error_message=traceback.format_exc(),
                    )
                except Exception:
                    pass
            return None

        if isinstance(exception, Exception):
            path = request.path
            method = request.method
            status_code = 500
            error_message = traceback.format_exc()

            # Save the error log to the database
            ErrorLog.objects.create(
                path=path,
                method=method,
                status_code=status_code,
                error_message=error_message,
            )
            return HttpResponse(
                "An error occurred. Please try again later.", status=status_code
            )
