"""Shared JSON responses, payload parsing, CSRF failures and admin access checks."""

import json
from functools import wraps

from django.http import JsonResponse

from ..roles import is_travel_admin


def error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def csrf_failure(request, reason=""):
    return JsonResponse(
        {
            "code": "csrf_failed",
            "error": "We could not verify this request. Your entered details are still here. Please try again.",
        },
        status=403,
    )


def body(request):
    try:
        data = json.loads(
            request.POST.get("payload", "{}")
            if request.content_type == "multipart/form-data"
            else request.body
        )
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid JSON body.")
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    return data


def staff_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_travel_admin(request.user):
            return error("Admin login required.", 403)
        return view(request, *args, **kwargs)

    return wrapped
