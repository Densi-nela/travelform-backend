"""Dashboard metrics and filtered request and cost analytics."""

import calendar
from datetime import date
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, TruncDate, TruncMonth
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from ..models import (
    AirTraveller,
    Notification,
    Ticket,
    TravelRequest,
)
from .common import error, staff_only
from .queries import awaiting_tickets, date_scope


@require_GET
@staff_only
def metrics(request):
    try:
        qs = date_scope(TravelRequest.objects.all(), request)
        month = date.fromisoformat(
            request.GET.get("month", timezone.localdate().strftime("%Y-%m")) + "-01"
        )
    except ValueError:
        return error("Choose a valid month or year.")
    monthly = qs.filter(created_at__year=month.year, created_at__month=month.month)
    daily = {
        row["day"].day: row["total"]
        for row in monthly.annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(total=Count("id"))
        .order_by("day")
    }
    buckets = {
        row["month"]: row["total"]
        for row in qs.order_by()
        .annotate(month=ExtractMonth("created_at"))
        .values("month")
        .annotate(total=Count("pk"))
    }
    return JsonResponse(
        {
            "total": qs.count(),
            "counts": {s: qs.filter(status=s).count() for s in TravelRequest.Status.values},
            "travellers": AirTraveller.objects.filter(request__in=qs).count(),
            "month_total": monthly.count(),
            "month": month.strftime("%Y-%m"),
            "available_years": [
                d.year for d in TravelRequest.objects.datetimes("created_at", "year", order="DESC")
            ],
            "awaiting_tickets": awaiting_tickets(qs).count(),
            "notification_failures": Notification.objects.filter(
                request__in=qs, delivered_at__isnull=True
            ).count(),
            "daily": [
                {"day": i, "requests": daily.get(i, 0)}
                for i in range(1, calendar.monthrange(month.year, month.month)[1] + 1)
            ],
            "monthly": [
                {"day": calendar.month_abbr[i], "requests": buckets.get(i, 0)} for i in range(1, 13)
            ],
        }
    )


@require_GET
@staff_only
def analytics(request):
    requested_year = request.GET.get("year")
    try:
        year = int(requested_year) if requested_year is not None else None
        if year is not None and not 1900 <= year <= 9998:
            raise ValueError
    except ValueError:
        return error("Choose a year between 1900 and 9998.")
    status = request.GET.get("status", "all")
    if status not in ["all", *TravelRequest.Status.values]:
        return error("Invalid status filter.")
    available = TravelRequest.objects.all()
    company = request.GET.get("company", "")
    department = request.GET.get("department", "")
    if company:
        if company not in dict(TravelRequest.COMPANIES):
            return error("Choose a valid company.")
        available = available.filter(company=company)
    if department:
        if not department.isdigit():
            return error("Choose a valid department.")
        available = available.filter(department_id=int(department))
    email = request.GET.get("email", "").strip().lower()
    if email:
        # Subquery avoids multiplying counts when someone is both applicant and traveller.
        matching = TravelRequest.objects.filter(
            Q(applicant_email__iexact=email) | Q(travellers__email__iexact=email)
        ).values("pk")
        available = available.filter(pk__in=matching)
    years = [d.year for d in available.datetimes("created_at", "year", order="DESC")]
    if requested_year is None:
        year = years[0] if years else None
    month = request.GET.get("month", "")
    from_month, to_month = request.GET.get("from_month", ""), request.GET.get("to_month", "")
    try:

        def parse_month(value):
            if len(value) != 7:
                raise ValueError
            result = date.fromisoformat(value + "-01")
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
        if start and (
            finish < start or (finish.year - start.year) * 12 + finish.month - start.month >= 120
        ):
            raise ValueError
    except ValueError:
        return error("Choose a valid month or an ordered month range of up to 10 years.")
    months = []
    if start:
        cursor = start
        while cursor <= finish:
            months.append(cursor)
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        qs = available.filter(created_at__date__gte=start, created_at__date__lt=cursor)
    else:
        qs = available.none()
    period_label = (
        (f"{start:%b %Y} – {finish:%b %Y}" if start != finish else f"{start:%b %Y}")
        if start
        else "No recorded period"
    )
    filtered = qs if status == "all" else qs.filter(status=status)
    counts = {s: filtered.filter(status=s).count() for s in TravelRequest.Status.values}
    buckets = {
        row["month"].strftime("%Y-%m"): row["total"]
        for row in filtered.order_by()
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(total=Count("pk"))
    }
    costs = Ticket.objects.filter(traveller__request__in=filtered, cost__isnull=False)

    def cost_rows(field, output):
        return [
            {output: row[field], "amount": str(row["amount"])}
            for row in costs.order_by()
            .values(field)
            .annotate(amount=Sum("cost"))
            .order_by("-amount")
        ]

    destinations = list(
        filtered.order_by()
        .values("destination")
        .annotate(requests=Count("pk"))
        .order_by("-requests", "destination")[:5]
    )
    travel_types = list(
        filtered.order_by()
        .values("travel_type")
        .annotate(requests=Count("pk"))
        .order_by("travel_type")
    )
    return JsonResponse(
        {
            "year": year,
            "status": status,
            "email": email,
            "available_years": years,
            "company": company,
            "department": department,
            "period_label": period_label,
            "month": month,
            "from_month": from_month,
            "to_month": to_month,
            "total_cost": str(costs.aggregate(amount=Sum("cost"))["amount"] or Decimal("0.00")),
            "costed_tickets": costs.count(),
            "cost_by_company": cost_rows("traveller__request__company", "company"),
            "cost_by_department": cost_rows("traveller__request__department__name", "department"),
            "total": filtered.count(),
            "filtered_total": filtered.count(),
            "counts": counts,
            "travellers": AirTraveller.objects.filter(request__in=filtered).count(),
            "monthly": [
                {
                    "month": d.month,
                    "label": d.strftime("%b %Y") if month or from_month else d.strftime("%b"),
                    "requests": buckets.get(d.strftime("%Y-%m"), 0),
                }
                for d in months
            ],
            "companies": list(
                filtered.order_by()
                .values("company")
                .annotate(requests=Count("pk"))
                .order_by("-requests")
            ),
            "departments": list(
                filtered.order_by()
                .values("department__name")
                .annotate(requests=Count("pk"))
                .order_by("-requests")
            ),
            "top_destinations": destinations,
            "travel_types": travel_types,
        }
    )
