import calendar
import json
from datetime import date
from functools import wraps
from uuid import UUID
from django.contrib.auth import authenticate, login, logout
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Count, Q, Case, When, Value, F
from django.db.models.functions import TruncDate, ExtractMonth
from django.http import JsonResponse, HttpResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .models import TravelRequest, AirTraveller, Notification, Ticket
from .notifications import deliver, deliver_many, queue_admin_review
from .validation import submission, text
from .email.errors import delivery_error_message


def error(message, status=400):
    return JsonResponse({'error': message}, status=status)


def csrf_failure(request, reason=''):
    return JsonResponse({'code': 'csrf_failed', 'error': 'We could not verify this request. Your entered details are still here. Please try again.'}, status=403)


def body(request):
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Invalid JSON body.')
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object.')
    return data


def staff_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_active or not request.user.is_staff:
            return error('Admin login required.', 403)
        return view(request, *args, **kwargs)
    return wrapped


def serialize(trip, detail=False):
    people = list(trip.travellers.all())
    result = {'id': str(trip.id), 'reference': f'APA-{str(trip.id)[:8].upper()}',
              'travel_type': trip.travel_type, 'applicant_name': trip.applicant_name,
              'applicant_email': trip.applicant_email, 'supervisor_email': trip.supervisor_email,
              'destination': trip.destination, 'status': trip.status, 'created_at': trip.created_at,
              'supervisor_status': trip.supervisor_status, 'supervisor_decided_at': trip.supervisor_decided_at, 'decided_at': trip.decided_at, 'traveller_count': len(people),
              'departure_date': min((p.departure_date for p in people), default=None)}
    if detail:
        result.update(event_description=trip.event_description, rejection_reason=trip.rejection_reason,
                      travellers=[{key: getattr(p, key) for key in ['id', 'name', 'email', 'departure_date',
                                  'departure_time', 'return_date', 'return_time', 'ticket_class']} for p in people])
    return result


@require_GET
def health(request):
    try:
        TravelRequest.objects.exists()
    except DatabaseError:
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})


@require_GET
def session(request):
    return JsonResponse({'csrf_token': get_token(request),
                         'is_admin': request.user.is_authenticated and request.user.is_staff,
                         'username': request.user.get_username() if request.user.is_authenticated else ''})


@require_POST
def sign_in(request):
    try:
        data = body(request)
        username = text(data, 'username', 150)
        password = data.get('password', '')
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise ValueError('Password is required.')
    except ValueError as exc:
        return error(str(exc))
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active or not user.is_staff:
        return error('Incorrect admin username or password.', 401)
    login(request, user)
    return JsonResponse({'is_admin': True, 'username': user.username, 'csrf_token': get_token(request)})


@require_POST
def sign_out(request):
    logout(request)
    return JsonResponse({'ok': True, 'csrf_token': get_token(request)})


@require_POST
def submit(request):
    try:
        data = body(request)
        fields, people = submission(data)
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        trip, created = TravelRequest.objects.get_or_create(submission_key=fields['submission_key'], defaults=fields)
        if created:
            AirTraveller.objects.bulk_create([AirTraveller(request=trip, **p) for p in people])
            messages = [Notification.objects.create(request=trip, event='supervisor_review', recipient=trip.supervisor_email),
                        Notification.objects.create(request=trip, event='applicant_receipt', recipient=trip.applicant_email)]
            transaction.on_commit(lambda: deliver_many([m.id for m in messages]))
    # Never disclose the approval credential, including in applicant receipts.
    return JsonResponse({'reference': f'APA-{str(trip.id)[:8].upper()}', 'status': trip.status,
                         'message': 'Request saved. Email notifications have been queued.'}, status=201 if created else 200)


@require_http_methods(['GET', 'POST'])
def review(request, token):
    trip = TravelRequest.objects.prefetch_related('travellers').filter(approval_token=token).exclude(supervisor_status='not_required').first()
    if trip is None:
        return error('This review link is invalid.', 404)
    if request.method == 'GET':
        response = JsonResponse(serialize(trip, True))
        response['Cache-Control'] = 'no-store'
        return response
    try:
        data = body(request)
        status = data.get('status')
        if status not in ['approved', 'rejected']:
            raise ValueError('Choose approve or reject.')
        reason = text(data, 'reason', 2000, optional=True) if status == 'rejected' else ''
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        # Compare-and-set prevents two tabs or repeated POSTs overwriting a decision.
        changed = TravelRequest.objects.filter(pk=trip.pk, status='pending').update(
            status='awaiting_admin' if status == 'approved' else 'rejected', supervisor_status=status,
            supervisor_decided_at=timezone.now(), rejection_reason=reason,
            decided_at=timezone.now() if status == 'rejected' else None)
        if not changed:
            return error('This request has already been decided. Refresh to see the result.', 409)
        notice = Notification.objects.create(request=trip, event='decision', recipient=trip.applicant_email, payload={'status': 'awaiting_admin' if status == 'approved' else 'rejected', 'reason': reason})
        notice_ids = [notice.id]
        if status == 'approved':
            notice_ids.extend(queue_admin_review(trip))
        transaction.on_commit(lambda: deliver_many(notice_ids))
    trip.refresh_from_db()
    return JsonResponse(serialize(trip, True))


def requests_queryset():
    return TravelRequest.objects.prefetch_related('travellers')


@require_GET
@staff_only
def requests_list(request):
    try:
        qs = date_scope(requests_queryset(), request)
    except ValueError as exc:
        return error(str(exc))
    search = request.GET.get('search', '').strip()[:200]
    if search:
        qs = qs.filter(Q(applicant_name__icontains=search) | Q(applicant_email__icontains=search) |
                       Q(destination__icontains=search) | Q(supervisor_email__icontains=search))
    status = request.GET.get('status', '')
    if status:
        if status not in TravelRequest.Status.values:
            return error('Invalid status filter.')
        qs = qs.filter(status=status)
    paginator = Paginator(qs, 10)
    page = paginator.get_page(request.GET.get('page', 1))
    return JsonResponse({'results': [serialize(t) for t in page], 'count': paginator.count,
                         'page': page.number, 'pages': paginator.num_pages})


@require_GET
@staff_only
def request_detail(request, pk):
    trip = requests_queryset().filter(pk=pk).first()
    if trip is None:
        return error('Request not found.', 404)
    result = serialize(trip, True)
    result['admin_message'] = trip.admin_message
    result['tickets'] = list(Ticket.objects.filter(traveller__request=trip).values('id', 'traveller_id', 'filename', 'size'))
    result['notifications'] = list(trip.notifications.values('id', 'event', 'recipient', 'delivered_at', 'attempts', 'last_error'))
    for notice in result['notifications']:
        notice['error_message'] = delivery_error_message(notice['last_error']) if notice['last_error'] else ''
    return JsonResponse(result)


@require_GET
@staff_only
def person_history(request):
    email = request.GET.get('email', '').strip().lower()
    if not email:
        return error('Email is required.')
    qs = requests_queryset().filter(Q(applicant_email__iexact=email) | Q(travellers__email__iexact=email)).distinct()
    counts = {s: qs.filter(status=s).count() for s in TravelRequest.Status.values}
    return JsonResponse({'email': email, 'total': qs.count(), 'counts': counts,
                         'results': [serialize(t) for t in qs[:50]]})


def date_scope(qs, request):
    year = request.GET.get('year')
    month = request.GET.get('month')
    try:
        if year:
            value = int(year)
            if not 1900 <= value <= 9998:
                raise ValueError
            return qs.filter(created_at__year=value)
        if month:
            value = date.fromisoformat(month + '-01')
            return qs.filter(created_at__year=value.year, created_at__month=value.month)
    except (ValueError, TypeError):
        raise ValueError('Choose a valid month or year.')
    return qs


@require_GET
@staff_only
def metrics(request):
    try:
        qs = date_scope(TravelRequest.objects.all(), request)
        month = date.fromisoformat(request.GET.get('month', timezone.localdate().strftime('%Y-%m')) + '-01')
    except ValueError:
        return error('Choose a valid month or year.')
    monthly = qs.filter(created_at__year=month.year, created_at__month=month.month)
    daily = {row['day'].day: row['total'] for row in monthly.annotate(day=TruncDate('created_at')).values('day').annotate(total=Count('id')).order_by('day')}
    buckets = {row['month']: row['total'] for row in qs.order_by().annotate(month=ExtractMonth('created_at')).values('month').annotate(total=Count('pk'))}
    return JsonResponse({'total': qs.count(), 'counts': {s: qs.filter(status=s).count() for s in TravelRequest.Status.values},
        'travellers': AirTraveller.objects.filter(request__in=qs).count(), 'month_total': monthly.count(), 'month': month.strftime('%Y-%m'),
        'available_years': [d.year for d in TravelRequest.objects.datetimes('created_at', 'year', order='DESC')],
        'notification_failures': Notification.objects.filter(request__in=qs, delivered_at__isnull=True).count(),
        'daily': [{'day': i, 'requests': daily.get(i, 0)} for i in range(1, calendar.monthrange(month.year, month.month)[1]+1)],
        'monthly': [{'day': calendar.month_abbr[i], 'requests': buckets.get(i, 0)} for i in range(1,13)]})


@require_GET
@staff_only
def analytics(request):
    requested_year = request.GET.get('year')
    try:
        year = int(requested_year) if requested_year is not None else None
        if year is not None and not 1900 <= year <= 9998:
            raise ValueError
    except ValueError:
        return error('Choose a year between 1900 and 9998.')
    status = request.GET.get('status', 'all')
    if status not in ['all', *TravelRequest.Status.values]:
        return error('Invalid status filter.')
    available = TravelRequest.objects.all()
    email = request.GET.get('email', '').strip().lower()
    if email:
        # Subquery avoids multiplying counts when someone is both applicant and traveller.
        matching = TravelRequest.objects.filter(
            Q(applicant_email__iexact=email) | Q(travellers__email__iexact=email)).values('pk')
        available = available.filter(pk__in=matching)
    years = [d.year for d in available.datetimes('created_at', 'year', order='DESC')]
    if requested_year is None:
        year = years[0] if years else None
    qs = available.filter(created_at__year=year) if year is not None else available.none()
    counts = {s: qs.filter(status=s).count() for s in TravelRequest.Status.values}
    filtered = qs if status == 'all' else qs.filter(status=status)
    buckets = {row['month']: row['total'] for row in filtered.order_by().annotate(
        month=ExtractMonth('created_at')).values('month').annotate(total=Count('pk'))}
    destinations = list(filtered.order_by().values('destination').annotate(
        requests=Count('pk')).order_by('-requests', 'destination')[:5])
    travel_types = list(filtered.order_by().values('travel_type').annotate(
        requests=Count('pk')).order_by('travel_type'))
    return JsonResponse({'year': year, 'status': status, 'email': email, 'available_years': years,
                         'total': qs.count(), 'filtered_total': filtered.count(), 'counts': counts,
                         'monthly': [{'month': i, 'label': calendar.month_abbr[i], 'requests': buckets.get(i, 0)}
                                     for i in range(1, 13)],
                         'top_destinations': destinations, 'travel_types': travel_types})


@require_POST
@staff_only
def admin_submit(request):
    try:
        fields, people = submission(body(request), admin=True)
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        trip, created = TravelRequest.objects.get_or_create(submission_key=fields['submission_key'],
            defaults={**fields, 'status': 'awaiting_admin', 'supervisor_status': 'not_required'})
        if created:
            AirTraveller.objects.bulk_create([AirTraveller(request=trip, **p) for p in people])
            notice = Notification.objects.create(request=trip, event='applicant_receipt', recipient=trip.applicant_email,
                payload={'status': 'awaiting_admin'})
            notice_ids = [notice.id, *queue_admin_review(trip)]
            transaction.on_commit(lambda: deliver_many(notice_ids))
    return JsonResponse(serialize(trip, True), status=201 if created else 200)


@require_POST
@staff_only
def admin_decision(request, pk):
    try:
        data = body(request)
        status = data.get('status')
        if status not in ['approved', 'rejected']:
            raise ValueError('Choose approve or reject.')
        message = text(data, 'message', 5000, optional=True)
        reason = text(data, 'reason', 2000, optional=True) if status == 'rejected' else ''
        personal = data.get('traveller_messages', {})
        if not isinstance(personal, dict):
            raise ValueError('Invalid traveller messages.')
        personal = {str(k): text({'message': v}, 'message', 5000, optional=True) for k, v in personal.items()}
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        # This write locks the request before checking travellers/tickets as well.
        allowed_statuses = ['pending', 'awaiting_admin'] if status == 'approved' else ['awaiting_admin']
        changed = TravelRequest.objects.filter(pk=pk, status__in=allowed_statuses).update(
            status=status, rejection_reason=reason, admin_message=message, decided_at=timezone.now(), admin_decided_by=request.user,
            supervisor_status=Case(When(status='pending', then=Value('bypassed')), default=F('supervisor_status')))
        if not changed:
            return error('Approve a pending request, or approve/reject a request awaiting admin action. Final decisions cannot be changed.', 409)
        trip = TravelRequest.objects.get(pk=pk)
        people = list(trip.travellers.all())
        if set(personal) - {str(p.id) for p in people}:
            transaction.set_rollback(True)
            return error('A traveller does not belong to this request.')
        recipients = {trip.applicant_email.lower(), *(p.email.lower() for p in people)}
        if trip.supervisor_email:
            recipients.add(trip.supervisor_email.lower())
        notices = []
        for recipient in sorted(recipients):
            own = [p for p in people if p.email.lower() == recipient]
            notes = [personal.get(str(p.id), '') for p in own]
            ticket_ids = list(Ticket.objects.filter(traveller__in=own).values_list('id', flat=True)) if status == 'approved' else []
            payload = {'status': status, 'reason': reason, 'message': '\n\n'.join(v for v in [message, *notes] if v),
                'direct_admin_approval': trip.supervisor_status == 'bypassed', 'ticket_ids': ticket_ids, 'employee_name': own[0].name if own else trip.applicant_name}
            notices.append(Notification.objects.create(request=trip, event='admin_decision', recipient=recipient, payload=payload).id)
        transaction.on_commit(lambda: deliver_many(notices))
    return JsonResponse(serialize(trip, True))


@require_POST
@staff_only
def upload_ticket(request, pk, traveller_id):
    uploads = request.FILES.getlist('ticket')
    if not uploads or len(uploads) > 50:
        return error('Choose between 1 and 50 attachments.')
    prepared = []
    for upload in uploads:
        if not upload.size or upload.size > 5 * 1024 * 1024:
            return error('Each attachment must be a PDF, PNG or JPEG up to 5 MB.')
        content = upload.read()
        if content.startswith(b'%PDF-'):
            mime = 'application/pdf'
        elif content.startswith(b'\x89PNG\r\n\x1a\n'):
            mime = 'image/png'
        elif content.startswith(b'\xff\xd8\xff'):
            mime = 'image/jpeg'
        else:
            return error('Attachments must be PDF, PNG or JPEG files. No files from this selection were saved.')
        prepared.append((upload.name[:200], mime, content))
        if sum(len(item[2]) for item in prepared) > 20 * 1024 * 1024:
            return error('A request supports up to 20 MB of attachments in total.')
    with transaction.atomic():
        if not TravelRequest.objects.filter(pk=pk, status__in=['pending', 'awaiting_admin']).update(status=F('status')):
            return error('Attachments can only be changed before a final decision.', 409)
        person = AirTraveller.objects.filter(pk=traveller_id, request_id=pk).first()
        if person is None:
            return error('Traveller not found.', 404)
        sizes = list(Ticket.objects.filter(traveller__request_id=pk).values_list('size', flat=True))
        if len(sizes) + len(prepared) > 50 or sum(sizes) + sum(len(item[2]) for item in prepared) > 20 * 1024 * 1024:
            return error('A request supports up to 50 files and 20 MB of attachments in total.')
        tickets = [Ticket.objects.create(traveller=person, filename=name, content_type=mime,
                   content=content, size=len(content)) for name, mime, content in prepared]
    # Keep the original single-file response fields for existing callers.
    return JsonResponse({'id': tickets[0].id, 'filename': tickets[0].filename,
                         'attachments': [{'id': t.id, 'filename': t.filename} for t in tickets]}, status=201)


@require_http_methods(['GET', 'DELETE'])
@staff_only
def ticket_detail(request, pk):
    ticket = Ticket.objects.select_related('traveller').filter(pk=pk).first()
    if ticket is None:
        return error('Ticket not found.', 404)
    if request.method == 'DELETE':
        with transaction.atomic():
            if not TravelRequest.objects.filter(pk=ticket.traveller.request_id, status__in=['pending', 'awaiting_admin']).update(status=F('status')):
                return error('Tickets on a decided request cannot be changed.', 409)
            ticket.delete()
        return JsonResponse({'ok': True})
    from django.utils.http import content_disposition_header
    response = HttpResponse(bytes(ticket.content), content_type=ticket.content_type)
    response['Content-Disposition'] = content_disposition_header(True, ticket.filename)
    response['X-Content-Type-Options'] = 'nosniff'
    return response


@require_POST
@staff_only
def retry_notification(request, pk, notification_id):
    notice = Notification.objects.filter(pk=notification_id, request_id=pk).first()
    if notice is None:
        return error('Email notification not found.', 404)
    if notice.delivered_at:
        return JsonResponse({'message': 'This email was already dispatched.'})
    if not deliver(notice.pk):
        notice.refresh_from_db()
        if notice.delivered_at:
            return JsonResponse({'message': 'This email was already dispatched.'})
        return error('This email is already being sent. Please refresh shortly.', 409)
    notice.refresh_from_db()
    if not notice.delivered_at:
        return error(delivery_error_message(notice.last_error), 502)
    return JsonResponse({'message': 'Email dispatched successfully.'})
