"""Render recipient-specific Django email templates. Do not put bearer links or other people's tickets in shared mail."""
from django.conf import settings
from django.utils import timezone
from django.template.loader import render_to_string

STATUS_LABELS = {'pending': 'Pending supervisor approval', 'awaiting_admin': 'Awaiting admin approval',
                 'approved': 'Approved', 'rejected': 'Rejected'}


def build_email_context(item, attachments):
    trip = item.request
    people = list(trip.travellers.all())
    event = item.event
    status = item.payload.get('status', trip.status if event in ('decision', 'admin_decision') else 'pending')
    recipient = item.recipient.lower()
    own = [p for p in people if p.email.lower() == recipient]
    role = item.payload.get('recipient_role')
    if not role:
        role = ('supervisor' if event == 'supervisor_review' else 'admin' if event == 'admin_review'
                else 'applicant' if recipient == trip.applicant_email.lower()
                else 'supervisor' if recipient == trip.supervisor_email.lower() else 'traveller')
    templates = {'applicant_receipt': 'applicant_receipt.html', 'supervisor_review': 'supervisor_review.html',
                 'decision': 'applicant_supervisor_decision.html', 'admin_review': 'admin_action_required.html', 'tickets': 'traveller_tickets.html'}
    template = templates.get(event)
    if event == 'admin_decision':
        template = f'{role}_final_decision.html'
    if not template:
        raise ValueError('Unknown travel email event.')
    if event == 'tickets':
        role = 'traveller'
    # Only the main contact, reviewers and admins see the complete submitted form.
    visible_people = own if role == 'traveller' else people
    reference = f'APA-{str(trip.pk)[:8].upper()}'
    title = {'applicant_receipt': 'Travel request received', 'supervisor_review': 'Travel request for your review',
             'admin_review': 'Travel request needs admin approval', 'tickets': 'Your travel tickets',
             'decision': 'Supervisor approved your request' if status == 'awaiting_admin' else 'Supervisor declined your request',
             'admin_decision': 'Travel request approved' if status == 'approved' else 'Travel request declined'}[event]
    greeting = ('Travel administrator' if role == 'admin' else 'Supervisor' if role == 'supervisor'
                else trip.applicant_name if role == 'applicant' else own[0].name if own else 'Traveller')
    data = {
        'direct_admin_approval': item.payload.get('direct_admin_approval', False), 'event': event, 'recipient_name': greeting, 'recipient_role': role, 'title': title, 'reference': reference,
        'status': status, 'status_label': STATUS_LABELS[status],
        'applicant_name': trip.applicant_name, 'applicant_email': trip.applicant_email,
        'supervisor_email': trip.supervisor_email, 'destination': trip.destination,
        'travel_type': trip.travel_type.replace('_', ' ').title(), 'event_description': trip.event_description,
        'submitted_at': timezone.localtime(trip.created_at).strftime('%d %b %Y, %H:%M %Z'),
        'traveller_count': str(len(people)),
        'travellers': [{'name': p.name, 'email': p.email, 'departure_date': p.departure_date.strftime('%d %b %Y'),
                        'departure_time': p.departure_time.strftime('%H:%M'), 'return_date': p.return_date.strftime('%d %b %Y'),
                        'return_time': p.return_time.strftime('%H:%M'), 'ticket_class': p.get_ticket_class_display()} for p in visible_people],
        'message': item.payload.get('message', ''), 'reason': item.payload.get('reason', ''),
        'attachments': [{'filename': a['filename'], 'type': 'Travel ticket'} for a in attachments],
        'logo_url': settings.APA_LOGO_URL,
    }
    # Review credentials never appear in receipts, admin mail or decision mail.
    if event == 'supervisor_review':
        data['review_url'] = f'{settings.FRONTEND_URL}/review/{trip.approval_token}'
    if event == 'admin_review':
        data['admin_url'] = f'{settings.FRONTEND_URL}/admin/requests/{trip.pk}'
    return {'schema_version': 1, 'event_id': f'travel-notification-{item.pk}', 'event': event,
            'recipient': item.recipient, 'subject': f'{title} | {reference} | {trip.destination}',
            'template': f'travel/email/{template}', 'template_data': data}


def build_email(item, attachments):
    """Render locally; the queue consumer receives ready-to-send HTML and plain text."""
    email = build_email_context(item, attachments)
    context = email.pop('template_data')
    email['subject'] = ' '.join(email['subject'].splitlines())
    email['html_body'] = render_to_string(email['template'], context)
    email['text_body'] = render_to_string('travel/email/plain.txt', context).strip()
    return email
