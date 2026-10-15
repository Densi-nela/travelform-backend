"""Travel request representations shared by public review and admin endpoints."""

from django.db.models import Q

from ..models import Ticket


def serialize(trip, detail=False):
    people = list(trip.travellers.all())
    result = {
        "id": str(trip.id),
        "reference": f"APA-{str(trip.id)[:8].upper()}",
        "company": trip.company,
        "department_id": trip.department_id,
        "department": trip.department.name if trip.department_id else "",
        "travel_type": trip.travel_type,
        "applicant_name": trip.applicant_name,
        "applicant_email": trip.applicant_email,
        "supervisor_email": trip.supervisor_email,
        "destination": trip.destination,
        "status": trip.status,
        "created_at": trip.created_at,
        "supervisor_status": trip.supervisor_status,
        "supervisor_decided_at": trip.supervisor_decided_at,
        "decided_at": trip.decided_at,
        "traveller_count": len(people),
        "departure_date": min((p.departure_date for p in people), default=None),
    }
    if detail:
        result.update(
            event_description=trip.event_description,
            rejection_reason=trip.rejection_reason,
            travellers=[
                {
                    key: getattr(p, key)
                    for key in [
                        "id",
                        "name",
                        "email",
                        "departure_date",
                        "departure_time",
                        "return_date",
                        "return_time",
                        "ticket_class",
                        "document_type",
                        "document_number",
                        "checked_luggage",
                    ]
                }
                for p in people
            ],
        )
    if detail:
        result["supporting_documents"] = list(
            trip.supporting_documents.values("id", "filename", "size")
        )
    if detail:
        for person in result["travellers"]:
            tickets = Ticket.objects.filter(traveller_id=person["id"])
            person["tickets_sent"] = (
                tickets.filter(notification__delivered_at__isnull=False).exists()
                and not tickets.filter(
                    Q(notification__isnull=True) | Q(notification__delivered_at__isnull=True)
                ).exists()
            )
    return result
