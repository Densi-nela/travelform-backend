import calendar
import json
from datetime import date
from functools import wraps
from uuid import UUID
from django.contrib.auth import authenticate, login, logout
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncDate, ExtractMonth
from django.http import JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from .models import TravelRequest, AirTraveller, Notification
from .notifications import deliver_many
from .validation import submission, text


def error(message, status=400):
    return JsonResponse({'error': message}, status=status)


def csrf_failure(request, reason=''):
    return error('Your session has expired. Refresh the page and try again.', 403)


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
              'decided_at': trip.decided_at, 'traveller_count': len(people),
              'departure_date': min((p.departure_date for p in people), default=None)}
    if detail:
        result.update(event_description=trip.event_description, rejection_reason=trip.rejection_reason,
                      travellers=[{key: getattr(p, key) for key in ['name', 'email', 'departure_date',
                                  'departure_time', 'return_date', 'return_time', 'ticket_class']} for p in people])
    return result


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
    trip = TravelRequest.objects.prefetch_related('travellers').filter(approval_token=token).first()
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
            status=status, rejection_reason=reason, decided_at=timezone.now())
        if not changed:
            return error('This request has already been decided. Refresh to see the result.', 409)
        notice = Notification.objects.create(request=trip, event='decision', recipient=trip.applicant_email)
        transaction.on_commit(lambda: deliver_many([notice.id]))
    trip.refresh_from_db()
    return JsonResponse(serialize(trip, True))


def requests_queryset():
    return TravelRequest.objects.prefetch_related('travellers')


@require_GET
@staff_only
def requests_list(request):
    qs = requests_queryset()
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
    result['notifications'] = list(trip.notifications.values('event', 'recipient', 'delivered_at', 'attempts', 'last_error'))
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


@require_GET
@staff_only
def metrics(request):
    try:
        month = date.fromisoformat(request.GET.get('month', timezone.localdate().strftime('%Y-%m')) + '-01')
    except ValueError:
        return error('Month must use YYYY-MM.')
    qs = TravelRequest.objects.all()
    monthly = qs.filter(created_at__year=month.year, created_at__month=month.month)
    daily = {row['day'].day: row['total'] for row in monthly.annotate(day=TruncDate('created_at')).values('day').annotate(total=Count('id')).order_by('day')}
    counts = {s: qs.filter(status=s).count() for s in TravelRequest.Status.values}
    return JsonResponse({'total': qs.count(), 'counts': counts, 'travellers': AirTraveller.objects.count(),
                         'month_total': monthly.count(), 'month': month.strftime('%Y-%m'),
                         'notification_failures': Notification.objects.filter(delivered_at__isnull=True).count(),
                         'daily': [{'day': i, 'requests': daily.get(i, 0)} for i in range(1, calendar.monthrange(month.year, month.month)[1] + 1)]})


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
