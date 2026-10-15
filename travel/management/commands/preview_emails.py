"""Render synthetic email previews without touching the database or sending mail."""

import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from django.core.management.base import BaseCommand

from travel.email.messages import build_email
from travel.models import AirTraveller


class Command(BaseCommand):
    help = "Render synthetic Django HTML/text email previews. No database writes or email delivery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output", required=True, help="Directory for the preview HTML, text and JSON files."
        )

    def handle(self, *args, **options):
        output = Path(options["output"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        people = [
            AirTraveller(
                name=name,
                email=email,
                departure_date=date(2026, 10, 15),
                departure_time=time(9),
                return_date=date(2026, 10, 17),
                return_time=time(17),
                ticket_class="economy",
            )
            for name, email in [
                ("Alex Example", "alex@example.com"),
                ("Sam Example", "sam@example.com"),
            ]
        ]
        trip = SimpleNamespace(
            pk=UUID("12345678-1234-4234-8234-123456789012"),
            approval_token=UUID("87654321-4321-4321-8321-210987654321"),
            applicant_name="Alex Example",
            applicant_email="alex@example.com",
            supervisor_email="supervisor@example.com",
            destination="Mombasa, Kenya",
            travel_type="air",
            event_description="Regional planning & partner workshop.\nDiscuss team objectives and branch development.",
            created_at=datetime(2026, 10, 5, 7, 30, tzinfo=timezone.utc),
            status="pending",
            travellers=SimpleNamespace(all=lambda: people),
        )
        scenarios = [
            ("applicant_receipt", "pending", "alex@example.com", False),
            ("applicant_receipt", "awaiting_admin", "alex@example.com", False),
            ("supervisor_review", "pending", "supervisor@example.com", False),
            ("decision", "awaiting_admin", "alex@example.com", False),
            ("decision", "rejected", "alex@example.com", False),
            ("admin_review", "awaiting_admin", "desk@example.com", False),
        ]
        scenarios += [
            (
                "admin_decision",
                status,
                recipient,
                status == "approved" and recipient != "supervisor@example.com",
            )
            for recipient in ["alex@example.com", "sam@example.com", "supervisor@example.com"]
            for status in ["approved", "rejected"]
        ]
        scenarios.append(("admin_decision", "approved", "sam@example.com", False))
        links = []
        for index, (event, status, recipient, with_ticket) in enumerate(scenarios, 1):
            trip.supervisor_email = (
                ""
                if event == "applicant_receipt" and status == "awaiting_admin"
                else "supervisor@example.com"
            )
            item = SimpleNamespace(
                pk=index,
                request=trip,
                event=event,
                recipient=recipient,
                payload={
                    "status": status,
                    "message": "Please arrive two hours before departure.\nContact the travel desk if your plans change."
                    if event == "admin_decision"
                    else "",
                    "reason": "Please reschedule after the budget review."
                    if status == "rejected"
                    else "",
                },
            )
            result = build_email(
                item, [{"filename": "Your-travel-ticket.pdf"}] if with_ticket else []
            )
            name = Path(result["template"]).stem + "-" + status + ("-yes" if with_ticket else "-no")
            (output / (name + ".html")).write_text(result["html_body"], encoding="utf-8")
            (output / (name + ".txt")).write_text(result["text_body"], encoding="utf-8")
            (output / (name + ".json")).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            links.append(
                f'<li><a href="{name}.html">{name}</a> (<a href="{name}.txt">plain text</a>)</li>'
            )
        (output / "index.html").write_text(
            '<!doctype html><html><head><meta charset="utf-8"><title>Travel email previews</title></head><body><h1>Travel email previews</h1><p>Synthetic examples rendered by Django. No emails were sent.</p><ul>'
            + "".join(links)
            + "</ul></body></html>",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Rendered {len(scenarios)} Django email scenarios to {output}. No emails sent."
            )
        )
