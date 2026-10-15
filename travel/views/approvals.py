"""Supervisor review and final travel-admin approval decisions."""

from django.db import transaction
from django.db.models import Case, F, Value, When
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from ..models import Notification, Ticket, TravelRequest
from ..notifications import deliver_many, queue_admin_review
from ..validation import text
from .common import body, error, staff_only
from .serializers import serialize


@require_http_methods(["GET", "POST"])
def review(request, token):
    trip = (
        TravelRequest.objects.prefetch_related("travellers")
        .filter(approval_token=token)
        .exclude(supervisor_status="not_required")
        .first()
    )
    if trip is None:
        return error("This review link is invalid.", 404)
    if request.method == "GET":
        response = JsonResponse(serialize(trip, True))
        response["Cache-Control"] = "no-store"
        return response
    try:
        data = body(request)
        status = data.get("status")
        if status not in ["approved", "rejected"]:
            raise ValueError("Choose approve or reject.")
        reason = text(data, "reason", 2000, optional=True) if status == "rejected" else ""
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        # Compare-and-set prevents two tabs or repeated POSTs overwriting a decision.
        changed = TravelRequest.objects.filter(pk=trip.pk, status="pending").update(
            status="awaiting_admin" if status == "approved" else "rejected",
            supervisor_status=status,
            supervisor_decided_at=timezone.now(),
            rejection_reason=reason,
            decided_at=timezone.now() if status == "rejected" else None,
        )
        if not changed:
            return error("This request has already been decided. Refresh to see the result.", 409)
        notice = Notification.objects.create(
            request=trip,
            event="decision",
            recipient=trip.applicant_email,
            payload={
                "status": "awaiting_admin" if status == "approved" else "rejected",
                "reason": reason,
            },
        )
        notice_ids = [notice.id]
        if status == "approved":
            notice_ids.extend(queue_admin_review(trip))
        transaction.on_commit(lambda: deliver_many(notice_ids))
    trip.refresh_from_db()
    return JsonResponse(serialize(trip, True))


@require_POST
@staff_only
def admin_decision(request, pk):
    try:
        data = body(request)
        status = data.get("status")
        if status not in ["approved", "rejected"]:
            raise ValueError("Choose approve or reject.")
        message = text(data, "message", 5000, optional=True)
        reason = text(data, "reason", 2000, optional=True) if status == "rejected" else ""
        personal = data.get("traveller_messages", {})
        if not isinstance(personal, dict):
            raise ValueError("Invalid traveller messages.")
        personal = {
            str(k): text({"message": v}, "message", 5000, optional=True)
            for k, v in personal.items()
        }
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        # This write locks the request before checking travellers/tickets as well.
        allowed_statuses = (
            ["pending", "awaiting_admin"] if status == "approved" else ["awaiting_admin"]
        )
        changed = TravelRequest.objects.filter(pk=pk, status__in=allowed_statuses).update(
            status=status,
            rejection_reason=reason,
            admin_message=message,
            decided_at=timezone.now(),
            admin_decided_by=request.user,
            supervisor_status=Case(
                When(status="pending", then=Value("bypassed")), default=F("supervisor_status")
            ),
        )
        if not changed:
            return error(
                "Approve a pending request, or approve/reject a request awaiting admin action. Final decisions cannot be changed.",
                409,
            )
        trip = TravelRequest.objects.get(pk=pk)
        people = list(trip.travellers.all())
        if set(personal) - {str(p.id) for p in people}:
            transaction.set_rollback(True)
            return error("A traveller does not belong to this request.")
        recipients = {trip.applicant_email.lower(), *(p.email.lower() for p in people)}
        if trip.supervisor_email:
            recipients.add(trip.supervisor_email.lower())
        notices = []
        for recipient in sorted(recipients):
            own = [p for p in people if p.email.lower() == recipient]
            notes = [personal.get(str(p.id), "") for p in own]
            ticket_ids = (
                list(Ticket.objects.filter(traveller__in=own).values_list("id", flat=True))
                if status == "approved"
                else []
            )
            payload = {
                "status": status,
                "reason": reason,
                "message": "\n\n".join(v for v in [message, *notes] if v),
                "direct_admin_approval": trip.supervisor_status == "bypassed",
                "ticket_ids": ticket_ids,
                "employee_name": own[0].name if own else trip.applicant_name,
            }
            notice = Notification.objects.create(
                request=trip, event="admin_decision", recipient=recipient, payload=payload
            )
            Ticket.objects.filter(pk__in=ticket_ids).update(notification=notice)
            notices.append(notice.id)
        transaction.on_commit(lambda: deliver_many(notices))
    return JsonResponse(serialize(trip, True))
