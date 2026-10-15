"""Traveller ticket uploads, downloads, costs and email dispatch."""

from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import F
from django.http import HttpResponse, JsonResponse
from django.views.decorators.http import require_http_methods, require_POST

from ..models import AirTraveller, Notification, Ticket, TravelRequest
from ..notifications import deliver_many
from ..validation import text
from .common import body, error, staff_only


@require_POST
@staff_only
def upload_ticket(request, pk, traveller_id):
    uploads = request.FILES.getlist("ticket")
    if not uploads or len(uploads) > 50:
        return error("Choose between 1 and 50 attachments.")
    prepared = []
    for upload in uploads:
        if not upload.size or upload.size > 5 * 1024 * 1024:
            return error("Each attachment must be a PDF, PNG or JPEG up to 5 MB.")
        content = upload.read()
        if content.startswith(b"%PDF-"):
            mime = "application/pdf"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            return error(
                "Attachments must be PDF, PNG or JPEG files. No files from this selection were saved."
            )
        prepared.append((upload.name[:200], mime, content))
        if sum(len(item[2]) for item in prepared) > 20 * 1024 * 1024:
            return error("A request supports up to 20 MB of attachments in total.")
    with transaction.atomic():
        if not TravelRequest.objects.filter(
            pk=pk, status__in=["pending", "awaiting_admin", "approved"]
        ).update(status=F("status")):
            return error("Attachments cannot be added to rejected requests.", 409)
        person = AirTraveller.objects.filter(pk=traveller_id, request_id=pk).first()
        if person is None:
            return error("Traveller not found.", 404)
        sizes = list(Ticket.objects.filter(traveller__request_id=pk).values_list("size", flat=True))
        if (
            len(sizes) + len(prepared) > 50
            or sum(sizes) + sum(len(item[2]) for item in prepared) > 20 * 1024 * 1024
        ):
            return error("A request supports up to 50 files and 20 MB of attachments in total.")
        tickets = [
            Ticket.objects.create(
                traveller=person,
                filename=name,
                content_type=mime,
                content=content,
                size=len(content),
            )
            for name, mime, content in prepared
        ]
    # Keep the original single-file response fields for existing callers.
    return JsonResponse(
        {
            "id": tickets[0].id,
            "filename": tickets[0].filename,
            "attachments": [{"id": t.id, "filename": t.filename} for t in tickets],
        },
        status=201,
    )


@require_http_methods(["GET", "DELETE", "PATCH"])
@staff_only
def ticket_detail(request, pk):
    ticket = Ticket.objects.select_related("traveller").filter(pk=pk).first()
    if ticket is None:
        return error("Ticket not found.", 404)
    if request.method == "PATCH":
        try:
            raw = body(request).get("cost")
            cost = None if raw is None or raw == "" else Decimal(str(raw))
            if cost is not None and (
                not cost.is_finite()
                or cost < 0
                or cost > Decimal("9999999999.99")
                or cost.as_tuple().exponent < -2
            ):
                raise ValueError("Enter a non-negative KES amount with at most two decimal places.")
        except (ValueError, InvalidOperation):
            return error("Enter a non-negative KES amount with at most two decimal places.")
        ticket.cost = cost
        ticket.save(update_fields=["cost"])
        return JsonResponse({"id": ticket.pk, "cost": ticket.cost})
    if request.method == "DELETE":
        with transaction.atomic():
            if not TravelRequest.objects.filter(
                pk=ticket.traveller.request_id, status__in=["pending", "awaiting_admin", "approved"]
            ).update(status=F("status")):
                return error("Attachments on rejected requests cannot be changed.", 409)
            ticket.refresh_from_db()
            if ticket.notification_id:
                return error(
                    "This attachment is already assigned to an email and cannot be removed.", 409
                )
            ticket.delete()
        return JsonResponse({"ok": True})
    from django.utils.http import content_disposition_header

    response = HttpResponse(bytes(ticket.content), content_type=ticket.content_type)
    response["Content-Disposition"] = content_disposition_header(True, ticket.filename)
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_POST
@staff_only
def send_tickets(request, pk):
    try:
        data = body(request)
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
        if not TravelRequest.objects.filter(pk=pk, status="approved").update(status=F("status")):
            return error("Tickets can only be sent for approved requests.", 409)
        trip = TravelRequest.objects.get(pk=pk)
        people = list(trip.travellers.all())
        if set(personal) - {str(p.id) for p in people}:
            return error("A traveller does not belong to this request.")
        unsent = list(
            Ticket.objects.filter(
                traveller__request=trip, notification__isnull=True
            ).select_related("traveller")
        )
        if not unsent:
            return error(
                "No new attachments to send. Retry any failed emails in Email delivery.", 409
            )
        ids = []
        for recipient in sorted({t.traveller.email.lower() for t in unsent}):
            own = [t for t in unsent if t.traveller.email.lower() == recipient]
            owners = {t.traveller_id for t in own}
            notice = Notification.objects.create(
                request=trip,
                event="tickets",
                recipient=recipient,
                payload={
                    "status": "approved",
                    "recipient_role": "traveller",
                    "ticket_ids": [t.pk for t in own],
                    "employee_name": own[0].traveller.name,
                    "message": "\n\n".join(
                        personal.get(str(p.id), "") for p in people if p.id in owners
                    ),
                },
            )
            Ticket.objects.filter(pk__in=[t.pk for t in own]).update(notification=notice)
            ids.append(notice.pk)
        transaction.on_commit(lambda: deliver_many(ids))
    return JsonResponse(
        {"message": "Ticket emails queued. Check Email delivery for their delivery status."}
    )
