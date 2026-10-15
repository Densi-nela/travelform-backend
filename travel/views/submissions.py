"""Public and travel-admin application submission endpoints."""

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from ..models import AirTraveller, Notification, TravelRequest
from ..notifications import deliver_many, queue_admin_review
from ..supporting_documents import (
    DocumentStorageError,
    document_batch,
    save_documents,
    validate_uploads,
)
from ..validation import submission
from .common import body, error, staff_only
from .serializers import serialize


@require_POST
def submit(request):
    try:
        data = body(request)
        fields, people = submission(data)
        files = validate_uploads(request)
    except ValueError as exc:
        return error(str(exc))
    try:
        with document_batch() as uploaded, transaction.atomic():
            trip, created = TravelRequest.objects.get_or_create(
                submission_key=fields["submission_key"], defaults=fields
            )
            if created:
                save_documents(trip, files, uploaded)
                AirTraveller.objects.bulk_create([AirTraveller(request=trip, **p) for p in people])
                messages = [
                    Notification.objects.create(
                        request=trip, event="supervisor_review", recipient=trip.supervisor_email
                    ),
                    Notification.objects.create(
                        request=trip, event="applicant_receipt", recipient=trip.applicant_email
                    ),
                ]
                transaction.on_commit(lambda: deliver_many([m.id for m in messages]))
    except DocumentStorageError as exc:
        return error(str(exc), 503)
    # Never disclose the approval credential, including in applicant receipts.
    return JsonResponse(
        {
            "reference": f"APA-{str(trip.id)[:8].upper()}",
            "status": trip.status,
            "message": "Request saved. Email notifications have been queued.",
        },
        status=201 if created else 200,
    )


@require_POST
@staff_only
def admin_submit(request):
    try:
        fields, people = submission(body(request), admin=True)
        files = validate_uploads(request)
    except ValueError as exc:
        return error(str(exc))
    try:
        with document_batch() as uploaded, transaction.atomic():
            trip, created = TravelRequest.objects.get_or_create(
                submission_key=fields["submission_key"],
                defaults={
                    **fields,
                    "status": "awaiting_admin",
                    "supervisor_status": "not_required",
                },
            )
            if created:
                save_documents(trip, files, uploaded)
                AirTraveller.objects.bulk_create([AirTraveller(request=trip, **p) for p in people])
                notice = Notification.objects.create(
                    request=trip,
                    event="applicant_receipt",
                    recipient=trip.applicant_email,
                    payload={"status": "awaiting_admin"},
                )
                notice_ids = [notice.id, *queue_admin_review(trip)]
                transaction.on_commit(lambda: deliver_many(notice_ids))
    except DocumentStorageError as exc:
        return error(str(exc), 503)
    return JsonResponse(serialize(trip, True), status=201 if created else 200)
