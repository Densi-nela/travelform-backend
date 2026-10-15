"""Shared travel querysets and date or outstanding-ticket filters."""

from datetime import date

from django.db.models import Q

from ..models import AirTraveller, Ticket, TravelRequest


def requests_queryset():
    return TravelRequest.objects.prefetch_related("travellers")


def date_scope(qs, request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    try:
        if year:
            value = int(year)
            if not 1900 <= value <= 9998:
                raise ValueError
            return qs.filter(created_at__year=value)
        if month:
            value = date.fromisoformat(month + "-01")
            return qs.filter(created_at__year=value.year, created_at__month=value.month)
    except (ValueError, TypeError):
        raise ValueError("Choose a valid month or year.")
    return qs


def awaiting_tickets(qs):
    delivered_people = AirTraveller.objects.filter(
        tickets__notification__delivered_at__isnull=False
    ).values("pk")
    missing = AirTraveller.objects.exclude(pk__in=delivered_people).values("request_id")
    outstanding = Ticket.objects.filter(
        Q(notification__isnull=True) | Q(notification__delivered_at__isnull=True)
    ).values("traveller__request_id")
    return qs.filter(status="approved").filter(Q(pk__in=missing) | Q(pk__in=outstanding))
