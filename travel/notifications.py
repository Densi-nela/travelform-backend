"""Durable notification delivery and retries; content and transports live in travel.email."""
import base64
import logging
from datetime import timedelta
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from .models import Notification, Ticket
from .email.messages import build_email
from .email.transports import dispatch_email
from .email.errors import delivery_error_code

logger = logging.getLogger(__name__)


def send_notification_email(supervisor_email: str, employee_name: str, travel_date, travel_link: str | None,
                            *, status='pending', event='supervisor_review', reason='', idempotency_key='',
                            message='', attachments=None, email=None):
    """Workflow hook retained for callers; template/model construction lives in email.messages.

    Ticket mail goes through email.transports.send_email_with_attachments.
    All other live mail goes to SQS. A complete template envelope is required.
    """
    if email is None or email['recipient'] != supervisor_email:
        raise ValueError('A matching recipient-specific email envelope is required.')
    dispatch_email(email, attachments or [])



def deliver(notification_id):
    # Atomically claim delivery across HTTP retries, initial sends and the worker.
    # Recover claims after a crashed process; normal API calls time out in 15 seconds.
    now = timezone.now()
    claimed = Notification.objects.filter(pk=notification_id, delivered_at__isnull=True).filter(
        Q(delivery_started_at__isnull=True) | Q(delivery_started_at__lt=now - timedelta(minutes=10))
    ).update(delivery_started_at=now)
    if not claimed:
        return False
    item = Notification.objects.select_related('request').get(pk=notification_id)
    trip = item.request
    link = f'{settings.FRONTEND_URL}/review/{trip.approval_token}' if item.event == 'supervisor_review' else None
    # Receipt/review always describe the submission, even if retried after a decision.
    status = item.payload.get('status', trip.status if item.event == 'decision' else 'pending')
    item.attempts += 1
    try:
        attachments = [{'filename': t.filename, 'content_type': t.content_type, 'content_base64': base64.b64encode(bytes(t.content)).decode('ascii')}
                       for t in Ticket.objects.filter(pk__in=item.payload.get('ticket_ids', []) if item.event == 'admin_decision' and status == 'approved' else [], traveller__request=trip, traveller__email__iexact=item.recipient)]
        send_notification_email(item.recipient, item.payload.get('employee_name', trip.applicant_name),
                                trip.travellers.order_by('departure_date').first().departure_date, link,
                                status=status, event=item.event,
                                reason=item.payload.get('reason', trip.rejection_reason if item.event == 'decision' else ''),
                                message=item.payload.get('message', ''), attachments=attachments,
                                idempotency_key=f'travel-notification-{item.pk}', email=build_email(item, attachments))
    except Exception as exc:
        item.last_error = delivery_error_code(exc)  # Do not store tokens or remote response bodies.
        logger.warning('Notification %s failed (%s); retry with retry_notifications.', item.pk, item.last_error)
    else:
        item.delivered_at = timezone.now()
        item.last_error = ''
    item.delivery_started_at = None
    item.save(update_fields=['attempts', 'last_error', 'delivered_at', 'delivery_started_at'])
    return True


def deliver_many(ids):
    for notification_id in ids:
        deliver(notification_id)


def queue_admin_review(trip):
    """Call inside the workflow transaction after supervisor approval or admin creation."""
    ids = []
    for recipient in sorted({value.strip().lower() for value in settings.TRAVEL_ADMIN_EMAILS if value.strip()}):
        from django.core.validators import validate_email
        validate_email(recipient)
        notice, _ = Notification.objects.get_or_create(request=trip, event='admin_review', recipient=recipient,
            defaults={'payload': {'status': 'awaiting_admin', 'recipient_role': 'admin'}})
        ids.append(notice.pk)
    return ids
