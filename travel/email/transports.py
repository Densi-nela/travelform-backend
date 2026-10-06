"""Delivery boundaries: adapt these functions to your notification consumer/service contracts."""
import json
from urllib.request import Request, urlopen
import boto3
from botocore.config import Config
from django.conf import settings


def build_sqs_message(email):
    """Map our rendered email envelope to your SQS consumer's schema here.

    Django has already rendered html_body and text_body; the consumer sends them.
    event_id is stable across retries and must be deduplicated by that consumer.
    """
    return email


def send_without_attachments(email):
    region = settings.NOTIFICATION_AWS_REGION
    if not region:
        raise ValueError('Configure AWS_REGION for notification delivery.')
    client = boto3.client('sqs', region_name=region,
                         config=Config(connect_timeout=3, read_timeout=10,
                                       retries={'mode': 'standard', 'total_max_attempts': 2}))
    queue_url = settings.NOTIFICATION_SQS_QUEUE_URL
    if not queue_url:
        queue_url = client.get_queue_url(QueueName=settings.NOTIFICATION_SQS_QUEUE_NAME)['QueueUrl']
    body = json.dumps(build_sqs_message(email), ensure_ascii=False)
    # Keep oversized content in the durable outbox rather than silently truncating.
    if len(body.encode('utf-8')) > 1_048_576:
        raise ValueError('Notification exceeds the SQS message size limit.')
    client.send_message(QueueUrl=queue_url, MessageBody=body)


def send_email_via_api(email, attachments=None):
    """Shared internal API adapter for plain emails and ticket emails.

    email contains recipient, subject, html_body and text_body. Each attachment
    contains filename, content_type and content_base64. Raise on delivery failure.
    """
    base_url = settings.NOTIFICATION_API_URL.strip().rstrip('/')
    if not base_url:
        raise ValueError('Configure NOTIFICATION_API_URL with the internal service base URL.')
    attachments = attachments or []
    payload = {'subject': email['subject'], 'text': email['html_body']}
    if not attachments:
        endpoint = 'sendEmail'
        payload.update(emailAddress=email['recipient'])
    elif len(attachments) == 1:
        endpoint = 'sendEmailAttachmentBytes'
        attachment = attachments[0]
        payload.update(emailAddress=email['recipient'], attachmentName=attachment['filename'],
                       bytes=attachment['content_base64'])
    else:
        endpoint = 'sendEmailMultiAttachment'
        payload.update(emailTo=email['recipient'], attachments=[
            {'name': item['filename'], 'bytes': item['content_base64']} for item in attachments
        ])
    # The outbox already base64-encodes ticket bytes; do not encode them again.
    headers = {'Content-Type': 'application/json'}
    # The supplied API contract does not require authentication. Retain optional
    # bearer-token support for deployments whose gateway requires it.
    if settings.NOTIFICATION_API_TOKEN:
        headers['Authorization'] = f'Bearer {settings.NOTIFICATION_API_TOKEN}'
    request = Request(f'{base_url}/notification/email/{endpoint}',
                      data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                      method='POST', headers=headers)
    with urlopen(request, timeout=15) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError('Notification service rejected the email.')


def send_email_with_attachments(email, attachments):
    """Ticket-mail customization hook, using the shared internal adapter by default."""
    send_email_via_api(email, attachments)


def dispatch_email(email, attachments):
    if settings.NOTIFICATION_MODE == 'console':
        if not settings.DEBUG:
            raise RuntimeError('Console notifications are development-only.')
        # No bearer URLs, personal messages, or attachment contents in logs.
        print('[DEVELOPMENT EMAIL] ' + json.dumps({'event_id': email['event_id'], 'template': email['template'],
              'recipient': email['recipient'], 'subject': email['subject'],
              'transport': 'internal_api' if attachments or settings.NOTIFICATION_USE_API_FOR_ALL_EMAILS else 'sqs',
              'attachment_count': len(attachments)}), flush=True)
        return
    if settings.NOTIFICATION_MODE not in ('live', 'api'):
        raise ValueError('NOTIFICATION_MODE must be console or live.')
    if attachments:
        send_email_with_attachments(email, attachments)
    elif settings.NOTIFICATION_USE_API_FOR_ALL_EMAILS:
        send_email_via_api(email)
    else:
        send_without_attachments(email)
