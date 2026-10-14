import calendar
import json
from decimal import Decimal, InvalidOperation
from datetime import date
from functools import wraps
from uuid import UUID
from django.contrib.auth import authenticate, login, logout
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Count, Q, Case, When, Value, F, Sum
from django.db.models.functions import TruncDate, ExtractMonth, TruncMonth
from django.http import JsonResponse, HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .models import TravelRequest, AirTraveller, Notification, Ticket, Department, SupportingDocument
from .notifications import deliver, deliver_many, queue_admin_review
from .validation import submission, text
from .email.errors import delivery_error_message
from .roles import is_travel_admin
from .supporting_documents import validate_uploads, document_batch, save_documents, download_url, DocumentStorageError


def error(message, status=400):
    return JsonResponse({'error': message}, status=status)


def csrf_failure(request, reason=''):
    return JsonResponse({'code': 'csrf_failed', 'error': 'We could not verify this request. Your entered details are still here. Please try again.'}, status=403)


def body(request):
    try:
        data = json.loads(request.POST.get('payload', '{}') if request.content_type == 'multipart/form-data' else request.body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError('Invalid JSON body.')
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object.')
    return data


def staff_only(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_travel_admin(request.user):
            return error('Admin login required.', 403)
        return view(request, *args, **kwargs)
    return wrapped


def serialize(trip, detail=False):
    people = list(trip.travellers.all())
    result = {'id': str(trip.id), 'reference': f'APA-{str(trip.id)[:8].upper()}',
              'company': trip.company, 'department_id': trip.department_id, 'department': trip.department.name if trip.department_id else '',
              'travel_type': trip.travel_type, 'applicant_name': trip.applicant_name,
              'applicant_email': trip.applicant_email, 'supervisor_email': trip.supervisor_email,
              'destination': trip.destination, 'status': trip.status, 'created_at': trip.created_at,
              'supervisor_status': trip.supervisor_status, 'supervisor_decided_at': trip.supervisor_decided_at, 'decided_at': trip.decided_at, 'traveller_count': len(people),
              'departure_date': min((p.departure_date for p in people), default=None)}
    if detail:
        result.update(event_description=trip.event_description, rejection_reason=trip.rejection_reason,
                      travellers=[{key: getattr(p, key) for key in ['id', 'name', 'email', 'departure_date',
                                  'departure_time', 'return_date', 'return_time', 'ticket_class', 'document_type', 'document_number', 'checked_luggage']} for p in people])
    if detail:
        result['supporting_documents'] = list(trip.supporting_documents.values('id', 'filename', 'size'))
    if detail:
        for person in result['travellers']:
            tickets = Ticket.objects.filter(traveller_id=person['id'])
            person['tickets_sent'] = tickets.filter(notification__delivered_at__isnull=False).exists() and not tickets.filter(Q(notification__isnull=True) | Q(notification__delivered_at__isnull=True)).exists()
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
                         'is_admin': is_travel_admin(request.user),
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
    if user is None or not is_travel_admin(user):
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
        files = validate_uploads(request)
    except ValueError as exc:
        return error(str(exc))
    try:
        with document_batch() as uploaded, transaction.atomic():
            trip, created = TravelRequest.objects.get_or_create(submission_key=fields['submission_key'], defaults=fields)
            if created:
                save_documents(trip, files, uploaded)
                AirTraveller.objects.bulk_create([AirTraveller(request=trip, **p) for p in people])
                messages = [Notification.objects.create(request=trip, event='supervisor_review', recipient=trip.supervisor_email),
                            Notification.objects.create(request=trip, event='applicant_receipt', recipient=trip.applicant_email)]
                transaction.on_commit(lambda: deliver_many([m.id for m in messages]))
    except DocumentStorageError as exc:
        return error(str(exc), 503)
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
    if status == 'awaiting_tickets':
        qs = awaiting_tickets(qs)
    elif status:
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
    result['tickets'] = list(Ticket.objects.filter(traveller__request=trip).values('id', 'traveller_id', 'filename', 'size', 'cost', 'notification_id', 'notification__delivered_at'))
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
        'awaiting_tickets': awaiting_tickets(qs).count(),
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
    company = request.GET.get('company', '')
    department = request.GET.get('department', '')
    if company:
        if company not in dict(TravelRequest.COMPANIES):
            return error('Choose a valid company.')
        available = available.filter(company=company)
    if department:
        if not department.isdigit():
            return error('Choose a valid department.')
        available = available.filter(department_id=int(department))
    email = request.GET.get('email', '').strip().lower()
    if email:
        # Subquery avoids multiplying counts when someone is both applicant and traveller.
        matching = TravelRequest.objects.filter(
            Q(applicant_email__iexact=email) | Q(travellers__email__iexact=email)).values('pk')
        available = available.filter(pk__in=matching)
    years = [d.year for d in available.datetimes('created_at', 'year', order='DESC')]
    if requested_year is None:
        year = years[0] if years else None
    month = request.GET.get('month', '')
    from_month, to_month = request.GET.get('from_month', ''), request.GET.get('to_month', '')
    try:
        def parse_month(value):
            if len(value) != 7:
                raise ValueError
            result = date.fromisoformat(value + '-01')
            if not 1900 <= result.year <= 9998:
                raise ValueError
            return result
        if month and (from_month or to_month):
            raise ValueError
        if month:
            start = finish = parse_month(month)
        elif from_month or to_month:
            start, finish = parse_month(from_month), parse_month(to_month)
        elif year is not None:
            start, finish = date(year, 1, 1), date(year, 12, 1)
        else:
            start = finish = None
        if start and (finish < start or (finish.year-start.year)*12+finish.month-start.month >= 120):
            raise ValueError
    except ValueError:
        return error('Choose a valid month or an ordered month range of up to 10 years.')
    months = []
    if start:
        cursor = start
        while cursor <= finish:
            months.append(cursor)
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        qs = available.filter(created_at__date__gte=start, created_at__date__lt=cursor)
    else:
        qs = available.none()
    period_label = (f'{start:%b %Y} – {finish:%b %Y}' if start != finish else f'{start:%b %Y}') if start else 'No recorded period'
    filtered = qs if status == 'all' else qs.filter(status=status)
    counts = {s: filtered.filter(status=s).count() for s in TravelRequest.Status.values}
    buckets = {row['month'].strftime('%Y-%m'): row['total'] for row in filtered.order_by().annotate(
        month=TruncMonth('created_at')).values('month').annotate(total=Count('pk'))}
    costs = Ticket.objects.filter(traveller__request__in=filtered, cost__isnull=False)
    def cost_rows(field, output):
        return [{output: row[field], 'amount': str(row['amount'])} for row in costs.order_by().values(field).annotate(amount=Sum('cost')).order_by('-amount')]
    destinations = list(filtered.order_by().values('destination').annotate(
        requests=Count('pk')).order_by('-requests', 'destination')[:5])
    travel_types = list(filtered.order_by().values('travel_type').annotate(
        requests=Count('pk')).order_by('travel_type'))
    return JsonResponse({'year': year, 'status': status, 'email': email, 'available_years': years,
                         'company': company, 'department': department, 'period_label': period_label,
                         'month': month, 'from_month': from_month, 'to_month': to_month,
                         'total_cost': str(costs.aggregate(amount=Sum('cost'))['amount'] or Decimal('0.00')),
                         'costed_tickets': costs.count(),
                         'cost_by_company': cost_rows('traveller__request__company', 'company'),
                         'cost_by_department': cost_rows('traveller__request__department__name', 'department'),
                         'total': filtered.count(), 'filtered_total': filtered.count(), 'counts': counts,
                         'travellers': AirTraveller.objects.filter(request__in=filtered).count(),
                         'monthly': [{'month': d.month, 'label': d.strftime('%b %Y') if month or from_month else d.strftime('%b'),
                                      'requests': buckets.get(d.strftime('%Y-%m'), 0)} for d in months],
                         'companies': list(filtered.order_by().values('company').annotate(requests=Count('pk')).order_by('-requests')),
                         'departments': list(filtered.order_by().values('department__name').annotate(requests=Count('pk')).order_by('-requests')),
                         'top_destinations': destinations, 'travel_types': travel_types})


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
            trip, created = TravelRequest.objects.get_or_create(submission_key=fields['submission_key'],
                defaults={**fields, 'status': 'awaiting_admin', 'supervisor_status': 'not_required'})
            if created:
                save_documents(trip, files, uploaded)
                AirTraveller.objects.bulk_create([AirTraveller(request=trip, **p) for p in people])
                notice = Notification.objects.create(request=trip, event='applicant_receipt', recipient=trip.applicant_email,
                    payload={'status': 'awaiting_admin'})
                notice_ids = [notice.id, *queue_admin_review(trip)]
                transaction.on_commit(lambda: deliver_many(notice_ids))
    except DocumentStorageError as exc:
        return error(str(exc), 503)
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
            notice = Notification.objects.create(request=trip, event='admin_decision', recipient=recipient, payload=payload)
            Ticket.objects.filter(pk__in=ticket_ids).update(notification=notice)
            notices.append(notice.id)
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
        if not TravelRequest.objects.filter(pk=pk, status__in=['pending', 'awaiting_admin', 'approved']).update(status=F('status')):
            return error('Attachments cannot be added to rejected requests.', 409)
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


@require_http_methods(['GET', 'DELETE', 'PATCH'])
@staff_only
def ticket_detail(request, pk):
    ticket = Ticket.objects.select_related('traveller').filter(pk=pk).first()
    if ticket is None:
        return error('Ticket not found.', 404)
    if request.method == 'PATCH':
        try:
            raw = body(request).get('cost')
            cost = None if raw is None or raw == '' else Decimal(str(raw))
            if cost is not None and (not cost.is_finite() or cost < 0 or cost > Decimal('9999999999.99') or cost.as_tuple().exponent < -2):
                raise ValueError('Enter a non-negative KES amount with at most two decimal places.')
        except (ValueError, InvalidOperation):
            return error('Enter a non-negative KES amount with at most two decimal places.')
        ticket.cost = cost
        ticket.save(update_fields=['cost'])
        return JsonResponse({'id': ticket.pk, 'cost': ticket.cost})
    if request.method == 'DELETE':
        with transaction.atomic():
            if not TravelRequest.objects.filter(pk=ticket.traveller.request_id, status__in=['pending', 'awaiting_admin', 'approved']).update(status=F('status')):
                return error('Attachments on rejected requests cannot be changed.', 409)
            ticket.refresh_from_db()
            if ticket.notification_id:
                return error('This attachment is already assigned to an email and cannot be removed.', 409)
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


@require_GET
@staff_only
def undelivered_notifications(request):
    try:
        trips = date_scope(TravelRequest.objects.all(), request)
    except ValueError as exc:
        return error(str(exc))
    notices = Notification.objects.filter(request__in=trips, delivered_at__isnull=True).select_related('request').order_by('-created_at', '-id')
    page = Paginator(notices, 20).get_page(request.GET.get('page', 1))
    return JsonResponse({'count': page.paginator.count, 'page': page.number, 'pages': page.paginator.num_pages,
        'results': [{'id': n.id, 'request_id': str(n.request_id),
                     'reference': f'APA-{str(n.request_id)[:8].upper()}', 'destination': n.request.destination,
                     'recipient': n.recipient, 'event': n.event, 'attempts': n.attempts,
                     'created_at': n.created_at, 'last_error': n.last_error,
                     'error_message': delivery_error_message(n.last_error) if n.last_error else ''}
                    for n in page]})


@require_GET
def department_options(request):
    qs = Department.objects.filter(active=True, name__icontains=request.GET.get('search', '')[:150])
    return JsonResponse({'results': list(qs.values('id', 'name')[:30])})


@require_GET
def supporting_document_download(request, pk, document_id, token=None):
    if token is not None:
        allowed = TravelRequest.objects.filter(pk=pk, approval_token=token).exclude(supervisor_status='not_required').exists()
    else:
        allowed = is_travel_admin(request.user)
    if not allowed:
        return error('Access denied.', 403)
    document = SupportingDocument.objects.filter(pk=document_id, request_id=pk).first()
    if document is None:
        return error('Document not found.', 404)
    try:
        response = HttpResponseRedirect(download_url(document))
        response['Cache-Control'] = 'no-store'
        response['Referrer-Policy'] = 'no-referrer'
        return response
    except Exception:
        return error('Document storage is unavailable. Please try again later.', 503)


def awaiting_tickets(qs):
    delivered_people = AirTraveller.objects.filter(tickets__notification__delivered_at__isnull=False).values('pk')
    missing = AirTraveller.objects.exclude(pk__in=delivered_people).values('request_id')
    outstanding = Ticket.objects.filter(Q(notification__isnull=True) | Q(notification__delivered_at__isnull=True)).values('traveller__request_id')
    return qs.filter(status='approved').filter(Q(pk__in=missing) | Q(pk__in=outstanding))


@require_POST
@staff_only
def send_tickets(request, pk):
    try:
        data = body(request)
        personal = data.get('traveller_messages', {})
        if not isinstance(personal, dict):
            raise ValueError('Invalid traveller messages.')
        personal = {str(k): text({'message': v}, 'message', 5000, optional=True) for k, v in personal.items()}
    except ValueError as exc:
        return error(str(exc))
    with transaction.atomic():
        if not TravelRequest.objects.filter(pk=pk, status='approved').update(status=F('status')):
            return error('Tickets can only be sent for approved requests.', 409)
        trip = TravelRequest.objects.get(pk=pk)
        people = list(trip.travellers.all())
        if set(personal) - {str(p.id) for p in people}:
            return error('A traveller does not belong to this request.')
        unsent = list(Ticket.objects.filter(traveller__request=trip, notification__isnull=True).select_related('traveller'))
        if not unsent:
            return error('No new attachments to send. Retry any failed emails in Email delivery.', 409)
        ids = []
        for recipient in sorted({t.traveller.email.lower() for t in unsent}):
            own = [t for t in unsent if t.traveller.email.lower() == recipient]
            owners = {t.traveller_id for t in own}
            notice = Notification.objects.create(request=trip, event='tickets', recipient=recipient, payload={
                'status': 'approved', 'recipient_role': 'traveller', 'ticket_ids': [t.pk for t in own],
                'employee_name': own[0].traveller.name,
                'message': '\n\n'.join(personal.get(str(p.id), '') for p in people if p.id in owners)})
            Ticket.objects.filter(pk__in=[t.pk for t in own]).update(notification=notice)
            ids.append(notice.pk)
        transaction.on_commit(lambda: deliver_many(ids))
    return JsonResponse({'message': 'Ticket emails queued. Check Email delivery for their delivery status.'})
