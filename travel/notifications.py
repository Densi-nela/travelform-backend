"""Replace send_notification_email with your internal notification API contract."""
import base64
import json
import logging
from urllib.request import Request, urlopen
from django.conf import settings
from django.utils import timezone
from .models import Notification, Ticket

logger = logging.getLogger(__name__)


def send_notification_email(supervisor_email: str, employee_name: str, travel_date, travel_link: str | None,
                            *, status='pending', event='supervisor_review', reason='', idempotency_key='', message='', attachments=None):
    """Same hook handles supervisor review, applicant receipt, and final decision.

    supervisor_email is the recipient (applicant email for applicant events).
    Only supervisor_review receives a private travel_link. The JSON contract below
    is an example: adapt it to your internal API. Raise on failure to enable retry.
    """
    payload = {'recipient': supervisor_email, 'employee_name': employee_name,
               'travel_date': str(travel_date), 'travel_link': travel_link,
               'status': status, 'event': event, 'reason': reason, 'message': message, 'attachments': attachments or []}
    if settings.NOTIFICATION_MODE == 'console':
        if not settings.DEBUG:
            raise RuntimeError('Console notifications are development-only.')
        print('[DEVELOPMENT EMAIL] ' + json.dumps({**payload, 'attachments': [{'filename': a['filename']} for a in payload['attachments']]}), flush=True)
        return
    if settings.NOTIFICATION_MODE != 'api' or not settings.NOTIFICATION_API_URL:
        raise RuntimeError('Configure the internal notification API.')
    req = Request(settings.NOTIFICATION_API_URL, data=json.dumps(payload).encode(), method='POST',
                  headers={'Content-Type': 'application/json',
                           'Authorization': f'Bearer {settings.NOTIFICATION_API_TOKEN}',
                           'Idempotency-Key': idempotency_key})
    with urlopen(req, timeout=10) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError('Notification service rejected the message.')


def deliver(notification_id):
    item = Notification.objects.select_related('request').get(pk=notification_id)
    if item.delivered_at:
        return
    trip = item.request
    link = f'{settings.FRONTEND_URL}/review/{trip.approval_token}' if item.event == 'supervisor_review' else None
    # Receipt/review always describe the submission, even if retried after a decision.
    status = item.payload.get('status', trip.status if item.event == 'decision' else 'pending')
    item.attempts += 1
    try:
        attachments = [{'filename': t.filename, 'content_type': t.content_type, 'content_base64': base64.b64encode(bytes(t.content)).decode('ascii')}
                       for t in Ticket.objects.filter(pk__in=item.payload.get('ticket_ids', []), traveller__request=trip, traveller__email__iexact=item.recipient)]
        send_notification_email(item.recipient, item.payload.get('employee_name', trip.applicant_name),
                                trip.travellers.order_by('departure_date').first().departure_date, link,
                                status=status, event=item.event,
                                reason=item.payload.get('reason', trip.rejection_reason if item.event == 'decision' else ''),
                                message=item.payload.get('message', ''), attachments=attachments,
                                idempotency_key=f'travel-notification-{item.pk}')
    except Exception as exc:
        item.last_error = type(exc).__name__  # Do not store tokens or remote response bodies.
        logger.warning('Notification %s failed (%s); retry with retry_notifications.', item.pk, item.last_error)
    else:
        item.delivered_at = timezone.now()
        item.last_error = ''
    item.save(update_fields=['attempts', 'last_error', 'delivered_at'])


def deliver_many(ids):
    for notification_id in ids:
        deliver(notification_id)
