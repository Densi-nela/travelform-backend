# Django travel email delivery

Django renders all email content in Python using `render_to_string`. Seven HTML templates extend a shared APA-branded base in `travel/templates/travel/email/`. Shared includes render journey details, traveller itineraries, messages, tickets and action buttons. A separate `plain.txt` template supplies the plain-text alternative. Dynamic HTML is escaped automatically, including personal messages and rejection reasons.

## Workflow

| Event | Recipient | HTML template |
| --- | --- | --- |
| `applicant_receipt` | Applicant / admin-created request's main contact | `applicant_receipt.html` |
| `supervisor_review` | Supervisor | `supervisor_review.html` |
| `decision` | Applicant after supervisor approval or rejection | `applicant_supervisor_decision.html` |
| `admin_review` | Configured travel administrators | `admin_action_required.html` |
| `admin_decision` | Applicant | `applicant_final_decision.html` |
| `admin_decision` | Other travellers | `traveller_final_decision.html` |
| `admin_decision` | Supervisor | `supervisor_final_decision.html` |

Submission receipts confirm success and include the full submitted form. Supervisor approval tells the applicant that the request is **waiting for admin approval**, and queues an action email for the configured admins. Admin-created requests skip supervisor review and send a receipt plus an admin action email. Decision templates handle both approval and rejection, including optional rejection reasons.

The applicant, supervisor and admin receive the complete request details. Other travellers receive only their own itinerary, not the other travellers' names or email addresses. Final decision recipients are deduplicated by email. Each traveller receives only tickets and personal messages assigned to their address. A shared email address receives the itineraries and tickets assigned to that address.

Only `supervisor_review` contains the private bearer review link. Admin action emails link to `/admin/requests/<uuid>`, which requires login. Receipts and final decision emails contain neither review credentials nor admin links.

## Delivery routes

- **Without attachments (default):** send the rendered email to the existing AWS SQS queue `notifications-email-queue`.
- **With attachments:** call `send_email_with_attachments(email, attachments)` in `travel/email/transports.py`. It delegates to the documented internal-service adapter.

Routing depends on the actual attachments for each recipient. A supervisor's final decision email normally uses SQS, while a traveller's ticket email uses the internal service. Rejections never include ticket attachments. The SQS consumer does not need any templates: it receives ready-to-send HTML and plain text.

## Configuration

Use `backend/.env` (see `.env.example`):

```dotenv
NOTIFICATION_MODE=live
AWS_REGION=your-aws-region
NOTIFICATION_SQS_QUEUE_NAME=notifications-email-queue
NOTIFICATION_SQS_QUEUE_URL=
NOTIFICATION_API_URL=http://notification-apis-service.service.apainsurance.ke:8080
FRONTEND_URL=https://travel.your-company.example
APA_LOGO_URL=https://www.apainsurance.org/images/AppResources/Apollo_Logo.png
```

Travel-admin recipients come from active Django users with the Travel admin role and a valid email address. Developers, inactive accounts and accounts without an email are excluded. Manage accounts through `/django-admin/auth/user/`. New events use the current user list; historical outbox records retain their original recipients.

`NOTIFICATION_MODE=console` remains the development default and sends nothing. Logs show delivery metadata only, excluding message contents, private review URLs and file data. `live` enables the real transports. The old `api` mode is accepted as an alias for `live`; the routing flag still selects the transport for attachment-free messages.

Run `uv sync --locked` to install the locked dependencies, including boto3. Use an IAM role or the standard AWS credential provider chain; no credentials are embedded in source. The app uses an existing **standard** queue and creates no AWS infrastructure. Allow `sqs:SendMessage` on the queue, plus `sqs:GetQueueUrl` if the URL is not configured. Setting `NOTIFICATION_SQS_QUEUE_URL` skips the name lookup. The consumer needs its own receive/delete permissions and sender/from configuration.

## Queue contract

Your existing queue consumer's JSON schema has not been supplied. The current explicit envelope is:

```json
{
  "schema_version": 1,
  "event_id": "travel-notification-123",
  "event": "applicant_receipt",
  "recipient": "alex@example.com",
  "subject": "Travel request received | APA-12345678 | Mombasa, Kenya",
  "template": "travel/email/applicant_receipt.html",
  "html_body": "<!DOCTYPE html>...fully rendered HTML...",
  "text_body": "Travel request received\n...plain-text alternative..."
}
```

Map these fields to your consumer's names in **`travel/email/transports.py::build_sqs_message(email)`**. The `template` field is diagnostic metadata only; the consumer sends `html_body` and `text_body` directly. It does not load a template or interpret Django variables. No attachment bytes are sent to SQS.

## Internal Notification API

The hook **`travel/email/transports.py::send_email_with_attachments(email, attachments)`** is implemented using the shared API adapter. `email` is the rendered envelope above. Each attachment contains:

```python
{
  "filename": "Alex-ticket.pdf",
  "content_type": "application/pdf",
  "content": b"%PDF-...",
}
```

The adapter posts JSON to the configured base URL using these documented endpoints:

| Attachments for this recipient | Endpoint under `/notification/email/` | Payload |
| --- | --- | --- |
| None | `sendEmail` | `emailAddress`, `subject`, `text` |
| One or multiple | `sendEmailMultiAttachment` | `emailTo`, `subject`, `text`, `attachments: [{name, bytes}]` |

`text` contains the rendered Django HTML. The outbox reads raw ticket bytes from the database. The API adapter serializes them in `bytes` as a JSON array of integers from 0 to 255 (for example `[37, 80, 68, 70, 45]`), not Base64. Retries read the stored bytes again and use the same array format. Tickets remain private in the database; no public attachment URL is needed. The service receives only its documented fields, not our internal event/template metadata. Plain text is retained for the SQS envelope but the internal API accepts a single body.

Requests use a 15-second timeout and propagate HTTP/network failures to the durable outbox. The supplied contract does not define idempotency support: a retry after a lost response can send a duplicate. API acceptance is not proof of inbox delivery.

## Code organization and retries

- `travel/email/messages.py`: recipient scope, stage-specific template selection, Django context and HTML/plain-text rendering.
- `travel/email/transports.py`: queue mapping, AWS SQS publishing and the custom attachment function.
- `travel/notifications.py`: durable outbox dispatch, delivery attempts and admin-recipient fan-out. The original workflow hook `send_notification_email` is retained and receives the rendered `email` envelope.
- `travel/views/submissions.py`, `approvals.py`, and `tickets.py`: create outbox records inside their workflow transactions.
- `travel/templates/travel/email/`: Django templates discovered through the app template loader.

Requests and outbox records commit together. Initial delivery is attempted after commit; failures remain retryable with a sanitized error class. Run `uv run --locked python manage.py retry_notifications` from one scheduled worker. Network retries and standard SQS can produce duplicates or out-of-order messages, so the consumer must deduplicate on `event_id`. A retry preserves the original stage and recorded messages rather than turning an old receipt into a later decision.

Dispatched means accepted by SQS or the internal service, not confirmed inbox delivery. Initial sends remain synchronous after commit; keep the attachment service quick to accept and queue messages for large groups. Files remain private in the database.

## Tests and previews

From `backend`:

```powershell
uv run --locked python manage.py test travel
uv run --locked python manage.py preview_emails --output ../../email-previews
```

The preview command renders 13 synthetic scenarios as HTML, text and example JSON, with an index page. It performs no database writes and sends no emails. Open the generated `index.html` locally to inspect the examples.

Tests cover template rendering and HTML escaping, recipient privacy, admin notifications, attachment routing, mocked SQS queue lookup/publishing and retry behavior. Tests assert the supplied internal API endpoint and payload contract for zero, one and multiple attachments. Live AWS/API delivery and rendering in Outlook/Gmail are not verified here.

Direct admin approval before supervisor review sends the same final notifications, with an explicit explanation that the administrator approved directly and no further supervisor action is required. The recorded supervisor state is `bypassed`; it is not treated as supervisor approval.


## Choose SQS or internal API for ordinary emails

Put `.env` in `backend/`, next to `manage.py`; Django loads this exact location.

```dotenv
NOTIFICATION_MODE=live
NOTIFICATION_USE_API_FOR_ALL_EMAILS=true
NOTIFICATION_API_URL=http://notification-apis-service.service.apainsurance.ke:8080
```

- `true`: all emails use the internal API; AWS credentials are unnecessary for email delivery.
- `false` (default): attachment-free emails use SQS; ticket emails use the internal API.
- `NOTIFICATION_MODE=console`: no real emails are sent, regardless of the flag.

The shared `travel/email/transports.py::send_email_via_api` adapter implements the supplied Notification API contract. Configure the **base URL**, without an email endpoint; the adapter selects the endpoint by attachment count. `send_email_with_attachments` delegates to it. The documented API needs no bearer token; `NOTIFICATION_API_TOKEN` is optional only if your gateway requires one. Django must be able to reach the internal service from its host/network. Restart Django after editing `.env`. The SQS consumer schema still needs confirmation in `build_sqs_message`; the API documentation does not define the queue contract.

For SQS, configure `AWS_REGION` and AWS credentials through an IAM role, AWS profile, or `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in your local backend `.env`. Temporary credentials also require `AWS_SESSION_TOKEN`. `NoCredentialsError` means boto3 could not locate usable credentials; a queue URL and region alone do not authenticate.

A successful submission means the request and notification outbox records were saved, not that email reached an inbox. Delivery errors leave messages pending for retry. Do not resubmit the form. After fixing configuration, run from `backend`:

```powershell
uv run --locked python manage.py retry_notifications
```

This retries all pending notifications using the current routing setting; it sends real messages when live mode is enabled. The retry command is not run automatically when changing configuration here.

## Admin email retry

Open an application in the admin dashboard and use **Email delivery → Retry email** beside the recipient. Only that undelivered notification is retried, with its original event snapshot, personal message and assigned tickets. The staff-only, CSRF-protected POST endpoint is `/api/admin/requests/<uuid>/notifications/<id>/retry/`. Successful deliveries cannot be resent here. Errors remain retryable and are shown without remote response bodies or credentials.

A shared database claim prevents overlapping initial, admin and worker sends. Claims expire after ten minutes to recover from a crashed process. The internal service still does not promise idempotency: if it accepts a message but its response is lost, a later retry can duplicate it. Apply migration 0003 before running the updated application.

Admins can select multiple PDF, PNG or JPEG attachments per traveller in one upload. The selection is validated and saved atomically. Limits remain 5 MB per file, 20 MB and 50 files per request. Each recipient receives only their assigned documents. Attachments are encoded in the backend and sent using the updated multi-attachment contract, including when there is just one file. Non-2xx HTTP responses and network failures leave the notification retryable.

Single and multiple attachments use the same `sendEmailMultiAttachment` contract. Attachment requests use `emailTo`; plain emails use `emailAddress`. MIME types remain internal metadata and are not included in the attachment API payload.
