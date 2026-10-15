"""Admin request lists, request details and traveller history."""

from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from ..email.errors import delivery_error_message
from ..models import Ticket, TravelRequest
from .common import error, staff_only
from .queries import awaiting_tickets, date_scope, requests_queryset
from .serializers import serialize


@require_GET
@staff_only
def requests_list(request):
    try:
        qs = date_scope(requests_queryset(), request)
    except ValueError as exc:
        return error(str(exc))
    search = request.GET.get("search", "").strip()[:200]
    if search:
        qs = qs.filter(
            Q(applicant_name__icontains=search)
            | Q(applicant_email__icontains=search)
            | Q(destination__icontains=search)
            | Q(supervisor_email__icontains=search)
        )
    status = request.GET.get("status", "")
    if status == "awaiting_tickets":
        qs = awaiting_tickets(qs)
    elif status:
        if status not in TravelRequest.Status.values:
            return error("Invalid status filter.")
        qs = qs.filter(status=status)
    paginator = Paginator(qs, 10)
    page = paginator.get_page(request.GET.get("page", 1))
    return JsonResponse(
        {
            "results": [serialize(t) for t in page],
            "count": paginator.count,
            "page": page.number,
            "pages": paginator.num_pages,
        }
    )


@require_GET
@staff_only
def request_detail(request, pk):
    trip = requests_queryset().filter(pk=pk).first()
    if trip is None:
        return error("Request not found.", 404)
    result = serialize(trip, True)
    result["admin_message"] = trip.admin_message
    result["tickets"] = list(
        Ticket.objects.filter(traveller__request=trip).values(
            "id",
            "traveller_id",
            "filename",
            "size",
            "cost",
            "notification_id",
            "notification__delivered_at",
        )
    )
    result["notifications"] = list(
        trip.notifications.values(
            "id", "event", "recipient", "delivered_at", "attempts", "last_error"
        )
    )
    for notice in result["notifications"]:
        notice["error_message"] = (
            delivery_error_message(notice["last_error"]) if notice["last_error"] else ""
        )
    return JsonResponse(result)


@require_GET
@staff_only
def person_history(request):
    email = request.GET.get("email", "").strip().lower()
    if not email:
        return error("Email is required.")
    qs = (
        requests_queryset()
        .filter(Q(applicant_email__iexact=email) | Q(travellers__email__iexact=email))
        .distinct()
    )
    counts = {s: qs.filter(status=s).count() for s in TravelRequest.Status.values}
    return JsonResponse(
        {
            "email": email,
            "total": qs.count(),
            "counts": counts,
            "results": [serialize(t) for t in qs[:50]],
        }
    )
