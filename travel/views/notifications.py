"""Undelivered email lists and individual notification retries."""

from django.core.paginator import Paginator
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST

from ..email.errors import delivery_error_message
from ..models import Notification, TravelRequest
from ..notifications import deliver
from .common import error, staff_only
from .queries import date_scope


@require_POST
@staff_only
def retry_notification(request, pk, notification_id):
    notice = Notification.objects.filter(pk=notification_id, request_id=pk).first()
    if notice is None:
        return error("Email notification not found.", 404)
    if notice.delivered_at:
        return JsonResponse({"message": "This email was already dispatched."})
    if not deliver(notice.pk):
        notice.refresh_from_db()
        if notice.delivered_at:
            return JsonResponse({"message": "This email was already dispatched."})
        return error("This email is already being sent. Please refresh shortly.", 409)
    notice.refresh_from_db()
    if not notice.delivered_at:
        return error(delivery_error_message(notice.last_error), 502)
    return JsonResponse({"message": "Email dispatched successfully."})


@require_GET
@staff_only
def undelivered_notifications(request):
    try:
        trips = date_scope(TravelRequest.objects.all(), request)
    except ValueError as exc:
        return error(str(exc))
    notices = (
        Notification.objects.filter(request__in=trips, delivered_at__isnull=True)
        .select_related("request")
        .order_by("-created_at", "-id")
    )
    page = Paginator(notices, 20).get_page(request.GET.get("page", 1))
    return JsonResponse(
        {
            "count": page.paginator.count,
            "page": page.number,
            "pages": page.paginator.num_pages,
            "results": [
                {
                    "id": n.id,
                    "request_id": str(n.request_id),
                    "reference": f"APA-{str(n.request_id)[:8].upper()}",
                    "destination": n.request.destination,
                    "recipient": n.recipient,
                    "event": n.event,
                    "attempts": n.attempts,
                    "created_at": n.created_at,
                    "last_error": n.last_error,
                    "error_message": delivery_error_message(n.last_error) if n.last_error else "",
                }
                for n in page
            ],
        }
    )
