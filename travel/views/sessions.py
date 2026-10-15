"""Backend health and travel-admin session endpoints."""

from django.contrib.auth import authenticate, login, logout
from django.db import DatabaseError
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.views.decorators.http import require_GET, require_POST

from ..models import TravelRequest
from ..roles import is_travel_admin
from ..validation import text
from .common import body, error


@require_GET
def health(request):
    try:
        TravelRequest.objects.exists()
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


@require_GET
def session(request):
    return JsonResponse(
        {
            "csrf_token": get_token(request),
            "is_admin": is_travel_admin(request.user),
            "username": request.user.get_username() if request.user.is_authenticated else "",
        }
    )


@require_POST
def sign_in(request):
    try:
        data = body(request)
        username = text(data, "username", 150)
        password = data.get("password", "")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise ValueError("Password is required.")
    except ValueError as exc:
        return error(str(exc))
    user = authenticate(request, username=username, password=password)
    if user is None or not is_travel_admin(user):
        return error("Incorrect admin username or password.", 401)
    login(request, user)
    return JsonResponse(
        {"is_admin": True, "username": user.username, "csrf_token": get_token(request)}
    )


@require_POST
def sign_out(request):
    logout(request)
    return JsonResponse({"ok": True, "csrf_token": get_token(request)})
