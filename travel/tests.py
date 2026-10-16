"""Regression coverage for travel workflows, permissions, delivery and reporting."""

import copy
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import AirTraveller, Department, Notification, SupportingDocument, TravelRequest
from .notifications import deliver


@override_settings(NOTIFICATION_MODE="console", NOTIFICATION_USE_API_FOR_ALL_EMAILS=False)
class TravelWorkflowTests(TestCase):
    def test_health_is_public_and_not_cached(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response["Cache-Control"], "no-store")

    @patch(
        "travel.views.sessions.TravelRequest.objects.exists",
        side_effect=DatabaseError("private database detail"),
    )
    def test_health_reports_database_unavailable_without_details(self, exists):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unavailable"})

    def setUp(self):
        departure = timezone.localdate() + timedelta(days=10)
        department = Department.objects.create(name="Operations")
        self.payload = {
            "company": "Apa General",
            "department_id": department.pk,
            "submission_key": str(uuid.uuid4()),
            "destination": "Mombasa, Kenya",
            "event_description": "Regional planning meeting",
            "applicant_name": "Jane Test",
            "applicant_email": "jane@example.com",
            "supervisor_email": "manager@example.com",
            "travellers": [
                {
                    "document_type": "national_id",
                    "document_number": "TEST1234",
                    "checked_luggage": True,
                    "name": "Jane Test",
                    "email": "jane@example.com",
                    "departure_date": departure.isoformat(),
                    "departure_time": "09:00",
                    "return_date": (departure + timedelta(days=2)).isoformat(),
                    "return_time": "17:00",
                    "ticket_class": "economy",
                }
            ],
        }

    def post(self, url, payload, client=None):
        return (client or self.client).post(
            url, json.dumps(payload), content_type="application/json"
        )

    def create(self):
        response = self.post("/api/requests/", self.payload)
        self.assertEqual(response.status_code, 201, response.content)
        return TravelRequest.objects.get()

    def admin(self):
        user = get_user_model().objects.create_user(
            username="admin", password="test-secret", is_staff=True
        )
        user.groups.add(Group.objects.get_or_create(name="Travel admins")[0])
        self.client.force_login(user)

    @patch("travel.notifications.send_notification_email")
    def test_submission_sends_two_distinct_emails_and_hides_credential(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post("/api/requests/", self.payload)
        trip = TravelRequest.objects.get()
        self.assertEqual(send.call_count, 2)
        supervisor, applicant = send.call_args_list
        self.assertEqual(supervisor.args[0], "manager@example.com")
        self.assertIn(str(trip.approval_token), supervisor.args[3])
        self.assertIsNone(applicant.args[3])
        self.assertEqual(applicant.args[0], "jane@example.com")
        self.assertNotIn(str(trip.approval_token), response.content.decode())
        self.assertNotEqual(trip.id, trip.approval_token)
        self.assertEqual(Notification.objects.filter(delivered_at__isnull=False).count(), 2)

    def test_retried_submission_is_idempotent(self):
        self.create()
        response = self.post("/api/requests/", self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TravelRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 2)

    @patch("travel.notifications.send_notification_email")
    def test_approval_and_duplicate_decision(self, send):
        trip = self.create()
        url = f"/api/review/{trip.approval_token}/"
        self.assertEqual(self.client.get(url).json()["status"], "pending")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(url, {"status": "approved"})
        self.assertEqual(response.json()["status"], "awaiting_admin")
        self.assertEqual(send.call_args.kwargs["status"], "awaiting_admin")
        self.assertEqual(send.call_args.args[0], "jane@example.com")
        self.assertIsNone(send.call_args.args[3])
        self.assertEqual(self.post(url, {"status": "rejected"}).status_code, 409)
        self.assertEqual(Notification.objects.filter(event="decision").count(), 1)
        trip.refresh_from_db()
        self.assertEqual(trip.status, "awaiting_admin")
        self.assertIsNone(trip.decided_at)
        self.assertEqual(trip.supervisor_status, "approved")
        self.assertIsNotNone(trip.supervisor_decided_at)

    def test_reject_with_and_without_reason(self):
        trip = self.create()
        response = self.post(
            f"/api/review/{trip.approval_token}/",
            {"status": "rejected", "reason": "Please reschedule."},
        )
        self.assertEqual(response.json()["rejection_reason"], "Please reschedule.")
        self.payload["submission_key"] = str(uuid.uuid4())
        self.post("/api/requests/", self.payload)
        other = TravelRequest.objects.exclude(pk=trip.pk).get()
        self.assertEqual(
            self.post(f"/api/review/{other.approval_token}/", {"status": "rejected"}).status_code,
            200,
        )

    def test_reference_does_not_grant_review_access(self):
        trip = self.create()
        self.assertEqual(self.client.get(f"/api/review/{trip.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/api/review/{uuid.uuid4()}/").status_code, 404)

    def test_validation(self):
        cases = [
            ({"supervisor_email": "not-an-email"}, "email"),
            ({"supervisor_email": "JANE@example.com"}, "differ"),
            ({"travellers": []}, "travellers"),
            ({"travel_type": "local"}, "air"),
            ({"destination": ""}, "Destination"),
            ({"travellers": [None]}, "Traveller"),
            ({"submission_key": "bad"}, "key"),
        ]
        for changes, message in cases:
            with self.subTest(changes=changes):
                response = self.post("/api/requests/", {**self.payload, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["error"])
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_date_validation_and_short_notice(self):
        data = copy.deepcopy(self.payload)
        data["travellers"][0]["return_date"] = data["travellers"][0]["departure_date"]
        data["travellers"][0]["return_time"] = "08:00"
        self.assertEqual(self.post("/api/requests/", data).status_code, 400)
        data = copy.deepcopy(self.payload)
        data["travellers"][0]["departure_date"] = (
            timezone.localdate() - timedelta(days=1)
        ).isoformat()
        self.assertEqual(self.post("/api/requests/", data).status_code, 400)
        data["travellers"][0]["departure_date"] = (
            timezone.localdate() + timedelta(days=1)
        ).isoformat()
        self.assertEqual(self.post("/api/requests/", data).status_code, 201)

    def test_admin_access_and_logout(self):
        self.create()
        for url in [
            "/api/admin/requests/",
            "/api/admin/metrics/",
            "/api/admin/people/?email=jane@example.com",
        ]:
            self.assertEqual(self.client.get(url).status_code, 403)
        user = get_user_model().objects.create_user(username="employee", password="test-secret")
        self.client.force_login(user)
        self.assertEqual(self.client.get("/api/admin/requests/").status_code, 403)
        self.assertEqual(
            self.post(
                "/api/login/", {"username": "employee", "password": "test-secret"}
            ).status_code,
            401,
        )
        self.admin()
        response = self.client.get("/api/admin/requests/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("approval_token", response.content.decode())
        self.assertEqual(response["Cache-Control"], "no-store")
        self.post("/api/logout/", {})
        self.assertEqual(self.client.get("/api/admin/metrics/").status_code, 403)

    def test_metrics_filter_order_and_person_history(self):
        first = self.create()
        self.payload["submission_key"] = str(uuid.uuid4())
        self.payload["applicant_email"] = "other@example.com"
        self.payload["destination"] = "Kisumu"
        self.post("/api/requests/", self.payload)
        self.admin()
        rows = self.client.get("/api/admin/requests/").json()
        self.assertEqual(rows["count"], 2)
        self.assertEqual(rows["results"][0]["destination"], "Kisumu")
        self.assertEqual(self.client.get("/api/admin/requests/?search=Mombasa").json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/requests/?status=approved").json()["count"], 0)
        metrics = self.client.get("/api/admin/metrics/").json()
        self.assertEqual(metrics["counts"]["pending"], 2)
        self.assertEqual(metrics["travellers"], 2)
        self.assertEqual(sum(p["requests"] for p in metrics["daily"]), 2)
        history = self.client.get("/api/admin/people/?email=JANE@example.com").json()
        self.assertEqual(history["total"], 2)  # applicant + traveller deduplicated
        detail = self.client.get(f"/api/admin/requests/{first.id}/").json()
        self.assertEqual(detail["travellers"][0]["email"], "jane@example.com")
        self.assertNotIn("approval_token", detail)

    @patch("travel.notifications.send_notification_email", side_effect=TimeoutError)
    def test_email_failure_keeps_request_and_supports_retry(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            trip = self.create()
        self.assertEqual(trip.status, "pending")
        notice = Notification.objects.get(event="supervisor_review")
        self.assertIsNone(notice.delivered_at)
        self.assertEqual(notice.attempts, 1)
        send.side_effect = None
        deliver(notice.pk)
        notice.refresh_from_db()
        self.assertIsNotNone(notice.delivered_at)
        self.assertEqual(notice.attempts, 2)
        self.assertEqual(
            send.call_args.kwargs["idempotency_key"], f"travel-notification-{notice.pk}"
        )
        calls = send.call_count
        deliver(notice.pk)
        self.assertEqual(send.call_count, calls)

    def test_csrf_is_enforced_on_public_and_admin_posts(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post("/api/requests/", self.payload, client).status_code, 403)
        token = client.get("/api/session/").json()["csrf_token"]
        response = client.post(
            "/api/requests/",
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 201)
        trip = TravelRequest.objects.get()
        self.assertEqual(
            self.post(
                f"/api/review/{trip.approval_token}/", {"status": "approved"}, client
            ).status_code,
            403,
        )
        self.assertEqual(
            self.post("/api/login/", {"username": "x", "password": "y"}, client).status_code, 403
        )

    def test_malformed_json(self):
        self.assertEqual(
            self.client.post("/api/requests/", "{", content_type="application/json").status_code,
            400,
        )
        self.assertEqual(self.post("/api/requests/", []).status_code, 400)

    def analytics_fixture(self):
        trip = self.create()
        # Duplicate traveller rows must not inflate a person's request counts.
        person = trip.travellers.first()
        person.pk = None
        person.save()
        TravelRequest.objects.filter(pk=trip.pk).update(
            created_at=timezone.make_aware(datetime(2025, 1, 15, 12))
        )
        self.payload["submission_key"] = str(uuid.uuid4())
        self.payload["applicant_email"] = "other@example.com"
        self.payload["travellers"][0]["email"] = "other@example.com"
        self.payload["destination"] = "Kisumu"
        self.post("/api/requests/", self.payload)
        second = TravelRequest.objects.exclude(pk=trip.pk).get()
        TravelRequest.objects.filter(pk=second.pk).update(
            status="approved", created_at=timezone.make_aware(datetime(2025, 12, 31, 23, 59))
        )
        self.payload["submission_key"] = str(uuid.uuid4())
        self.post("/api/requests/", self.payload)
        TravelRequest.objects.exclude(pk__in=[trip.pk, second.pk]).update(
            status="rejected", created_at=timezone.make_aware(datetime(2026, 1, 1, 0, 1))
        )
        self.admin()

    def test_annual_analytics_zero_fills_and_respects_year_boundaries(self):
        self.analytics_fixture()
        data = self.client.get("/api/admin/analytics/?year=2025").json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["monthly"]), 12)
        self.assertEqual(data["monthly"][0]["requests"], 1)
        self.assertEqual(data["monthly"][11]["requests"], 1)
        self.assertEqual(data["monthly"][1]["requests"], 0)
        self.assertEqual(
            data["counts"], {"pending": 1, "awaiting_admin": 0, "approved": 1, "rejected": 0}
        )
        self.assertEqual(self.client.get("/api/admin/analytics/?year=2026").json()["total"], 1)
        self.assertIn(2025, data["available_years"])

    def test_analytics_status_filters_every_aggregate(self):
        self.analytics_fixture()
        data = self.client.get("/api/admin/analytics/?year=2025&status=approved").json()
        self.assertEqual(data["filtered_total"], 1)
        self.assertEqual(sum(m["requests"] for m in data["monthly"]), 1)
        self.assertEqual(data["counts"]["pending"], 0)
        self.assertEqual(data["top_destinations"], [{"destination": "Kisumu", "requests": 1}])
        empty = self.client.get("/api/admin/analytics/?year=2024&status=rejected").json()
        self.assertEqual(empty["total"], 0)
        self.assertTrue(all(m["requests"] == 0 for m in empty["monthly"]))

    def test_employee_analytics_scope_and_deduplication(self):
        self.analytics_fixture()
        data = self.client.get("/api/admin/analytics/?year=2025&email=JANE@example.com").json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["filtered_total"], 1)
        self.assertEqual(sum(m["requests"] for m in data["monthly"]), 1)
        self.assertEqual(
            data["top_destinations"], [{"destination": "Mombasa, Kenya", "requests": 1}]
        )

    def test_analytics_requires_staff_and_valid_filters(self):
        self.assertEqual(self.client.get("/api/admin/analytics/?year=2025").status_code, 403)
        self.admin()
        for query in ["year=abc", "year=0", "year=9999", "status=unknown"]:
            self.assertEqual(self.client.get("/api/admin/analytics/?" + query).status_code, 400)

    def test_analytics_years_only_include_scoped_records(self):
        self.analytics_fixture()
        data = self.client.get("/api/admin/analytics/?year=2024").json()
        self.assertEqual(data["available_years"], [2026, 2025])
        self.assertEqual(self.client.get("/api/admin/analytics/").json()["year"], 2026)
        person = self.client.get("/api/admin/analytics/?email=jane@example.com").json()
        self.assertEqual(person["available_years"], [2025])
        self.assertEqual(person["year"], 2025)
        self.assertEqual(person["total"], 1)

    def test_analytics_no_records_has_no_invented_year(self):
        self.admin()
        data = self.client.get("/api/admin/analytics/").json()
        self.assertEqual(data["available_years"], [])
        self.assertIsNone(data["year"])
        self.assertEqual(data["travel_types"], [])
        self.assertEqual(data["total"], 0)

    def test_travel_type_distribution_respects_year_status_and_person(self):
        self.analytics_fixture()
        TravelRequest.objects.filter(status="approved").update(travel_type="local")
        data = self.client.get("/api/admin/analytics/?year=2025").json()
        self.assertEqual(
            data["travel_types"],
            [{"travel_type": "air", "requests": 1}, {"travel_type": "local", "requests": 1}],
        )
        selected = self.client.get("/api/admin/analytics/?year=2025&status=approved").json()
        self.assertEqual(selected["travel_types"], [{"travel_type": "local", "requests": 1}])
        person = self.client.get("/api/admin/analytics/?email=jane@example.com").json()
        self.assertEqual(person["travel_types"], [{"travel_type": "air", "requests": 1}])
        self.assertEqual(sum(t["requests"] for t in data["travel_types"]), data["filtered_total"])

    def awaiting_admin(self):
        trip = self.create()
        self.post(f"/api/review/{trip.approval_token}/", {"status": "approved"})
        trip.refresh_from_db()
        return trip

    @patch("travel.notifications.send_notification_email")
    def test_admin_approval_fanout_private_tickets_and_retry_snapshot(self, send):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import Ticket

        self.payload["travellers"].append(
            {
                **self.payload["travellers"][0],
                "name": "Other Traveller",
                "email": "other@example.com",
            }
        )
        trip = self.awaiting_admin()
        self.admin()
        people = list(trip.travellers.all())
        for person in people:
            response = self.client.post(
                f"/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/",
                {
                    "ticket": SimpleUploadedFile(
                        f"{person.id}.pdf",
                        b"%PDF-1.7 sample ticket",
                        content_type="application/pdf",
                    )
                },
            )
            self.assertEqual(response.status_code, 201)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(
                f"/api/admin/requests/{trip.pk}/decision/",
                {
                    "status": "approved",
                    "message": "Have a good trip.",
                    "traveller_messages": {
                        str(people[0].id): "Jane instructions",
                        str(people[1].id): "Other instructions",
                    },
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "approved")
        trip.refresh_from_db()
        self.assertIsNotNone(trip.admin_decided_by_id)
        calls = {call.args[0]: call for call in send.call_args_list}
        self.assertEqual(
            set(calls), {"jane@example.com", "other@example.com", "manager@example.com"}
        )
        self.assertEqual(calls["manager@example.com"].kwargs["attachments"], [])
        self.assertEqual(calls["manager@example.com"].kwargs["message"], "Have a good trip.")
        for person in people:
            call = calls[person.email]
            self.assertEqual(len(call.kwargs["attachments"]), 1)
            self.assertEqual(call.kwargs["attachments"][0]["filename"], f"{person.id}.pdf")
            self.assertIsNone(call.args[3])
        self.assertNotIn("Other instructions", calls["jane@example.com"].kwargs["message"])
        self.assertEqual(
            self.post(
                f"/api/admin/requests/{trip.pk}/decision/", {"status": "rejected"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.delete(f"/api/admin/tickets/{Ticket.objects.first().pk}/").status_code, 409
        )
        # Delayed supervisor notification must still describe its own stage.
        deliver(Notification.objects.get(request=trip, event="decision").pk)
        self.assertEqual(send.call_args.kwargs["status"], "awaiting_admin")

    def test_admin_create_skips_supervisor_and_requires_staff(self):
        payload = {**self.payload}
        del payload["supervisor_email"]
        self.assertEqual(self.post("/api/admin/requests/new/", payload).status_code, 403)
        self.assertEqual(self.post("/api/requests/", payload).status_code, 400)
        self.admin()
        response = self.post("/api/admin/requests/new/", payload)
        self.assertEqual(response.status_code, 201)
        trip = TravelRequest.objects.get()
        self.assertEqual(trip.supervisor_status, "not_required")
        self.assertEqual(trip.status, "awaiting_admin")
        self.assertEqual(trip.supervisor_email, "")
        self.assertFalse(trip.notifications.filter(event="supervisor_review").exists())
        self.assertEqual(self.client.get(f"/api/review/{trip.approval_token}/").status_code, 404)
        self.assertEqual(self.post("/api/admin/requests/new/", payload).status_code, 200)

    def test_admin_rejection_waits_for_supervisor_and_blocks_foreign_traveller(self):
        trip = self.create()
        url = f"/api/admin/requests/{trip.pk}/decision/"
        self.assertEqual(self.post(url, {"status": "approved"}).status_code, 403)
        self.admin()
        self.assertEqual(self.post(url, {"status": "rejected"}).status_code, 409)
        self.post(f"/api/review/{trip.approval_token}/", {"status": "approved"})
        self.assertEqual(
            self.post(
                url, {"status": "approved", "traveller_messages": {"999999": "foreign"}}
            ).status_code,
            400,
        )
        trip.refresh_from_db()
        self.assertEqual(trip.status, "awaiting_admin")
        self.assertFalse(trip.notifications.filter(event="admin_decision").exists())

    @patch("travel.notifications.send_notification_email")
    def test_admin_rejection_notifies_supervisor_and_applicant_without_tickets(self, send):
        trip = self.awaiting_admin()
        self.admin()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(
                f"/api/admin/requests/{trip.pk}/decision/",
                {"status": "rejected", "reason": "Budget unavailable"},
            )
        self.assertEqual(response.json()["rejection_reason"], "Budget unavailable")
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertEqual(call.kwargs["status"], "rejected")
            self.assertEqual(call.kwargs["reason"], "Budget unavailable")
            self.assertEqual(call.kwargs["attachments"], [])

    def test_ticket_validation_private_download_and_late_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        trip = self.awaiting_admin()
        person = trip.travellers.first()
        url = f"/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/"
        self.assertEqual(self.client.post(url).status_code, 403)
        self.admin()
        self.assertEqual(
            self.client.post(
                url, {"ticket": SimpleUploadedFile("bad.pdf", b"<html>bad</html>")}
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                url,
                {"ticket": SimpleUploadedFile("large.pdf", b"%PDF-" + b"a" * (5 * 1024 * 1024))},
            ).status_code,
            400,
        )
        ticket = self.client.post(
            url, {"ticket": SimpleUploadedFile("ticket.pdf", b"%PDF-1.7 sample")}
        ).json()
        download = f"/api/admin/tickets/{ticket['id']}/"
        self.assertEqual(self.client.get(download)["Cache-Control"], "no-store")
        self.assertIn("attachment", self.client.get(download)["Content-Disposition"])
        self.client.logout()
        self.assertEqual(self.client.get(download).status_code, 403)
        self.client.force_login(get_user_model().objects.get(username="admin"))
        self.post(f"/api/admin/requests/{trip.pk}/decision/", {"status": "approved"})
        self.assertEqual(
            self.client.post(
                url, {"ticket": SimpleUploadedFile("ticket.pdf", b"%PDF-1.7 sample")}
            ).status_code,
            201,
        )

    def test_global_date_filter_scopes_metrics_list_and_notifications(self):
        trip = self.awaiting_admin()
        TravelRequest.objects.filter(pk=trip.pk).update(
            created_at=timezone.make_aware(datetime(2024, 2, 14, 12))
        )
        self.payload["submission_key"] = str(uuid.uuid4())
        self.post("/api/requests/", self.payload)
        self.admin()
        for query in ["year=2024", "month=2024-02"]:
            metrics = self.client.get("/api/admin/metrics/?" + query).json()
            rows = self.client.get("/api/admin/requests/?" + query).json()
            self.assertEqual(metrics["total"], 1)
            self.assertEqual(rows["count"], 1)
            self.assertEqual(metrics["counts"]["awaiting_admin"], 1)
            self.assertEqual(metrics["travellers"], 1)
            self.assertEqual(metrics["notification_failures"], 3)
        self.assertEqual(sum(x["requests"] for x in metrics["daily"]), 1)
        self.assertEqual(self.client.get("/api/admin/requests/?year=invalid").status_code, 400)
        self.assertEqual(self.client.get("/api/admin/metrics/?month=bad").status_code, 400)

    def test_upgrade_preserves_supervisor_decisions_and_retry_meaning(self):
        from importlib import import_module

        from django.apps import apps

        trip = self.create()
        decided = timezone.now()
        TravelRequest.objects.filter(pk=trip.pk).update(status="approved", decided_at=decided)
        notice = Notification.objects.create(
            request=trip, event="decision", recipient=trip.applicant_email
        )
        import_module("travel.migrations.0002_ticket_and_more").migrate_stages(apps, None)
        trip.refresh_from_db()
        notice.refresh_from_db()
        self.assertEqual(trip.status, "awaiting_admin")
        self.assertEqual(trip.supervisor_status, "approved")
        self.assertEqual(trip.supervisor_decided_at, decided)
        self.assertIsNone(trip.decided_at)
        self.assertEqual(notice.payload["status"], "awaiting_admin")

    @patch("travel.notifications.send_notification_email")
    def test_admin_review_notice_is_deduplicated_and_has_login_link_only(self, send):
        for name, email in [("desk1", "desk@example.com"), ("desk2", "DESK@example.com")]:
            user = get_user_model().objects.create_user(username=name, email=email)
            user.groups.add(Group.objects.get_or_create(name="Travel admins")[0])
        trip = self.create()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(f"/api/review/{trip.approval_token}/", {"status": "approved"})
        self.assertEqual(response.status_code, 200)
        notice = Notification.objects.get(event="admin_review")
        self.assertEqual(notice.recipient, "desk@example.com")
        calls = {c.args[0]: c.kwargs["email"] for c in send.call_args_list}
        self.assertEqual(
            calls["desk@example.com"]["template"], "travel/email/admin_action_required.html"
        )
        admin_html = calls["desk@example.com"]["html_body"]
        self.assertIn("Awaiting admin approval", admin_html)
        self.assertIn(f"/admin/requests/{trip.pk}", admin_html)
        self.assertNotIn(str(trip.approval_token), json.dumps(calls))
        self.assertEqual(
            calls["jane@example.com"]["template"], "travel/email/applicant_supervisor_decision.html"
        )
        self.assertEqual(
            self.post(f"/api/review/{trip.approval_token}/", {"status": "approved"}).status_code,
            409,
        )
        self.assertEqual(Notification.objects.filter(event="admin_review").count(), 1)

    def test_admin_created_request_queues_admin_email(self):
        self.admin()
        get_user_model().objects.filter(username="admin").update(email="desk@example.com")
        response = self.post("/api/admin/requests/new/", self.payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Notification.objects.filter(event="admin_review").count(), 1)
        self.assertFalse(Notification.objects.filter(event="supervisor_review").exists())

    def test_email_models_are_recipient_scoped_and_receipt_is_stable(self):
        from .email.messages import build_email_context as build_email

        self.payload["travellers"].append(
            {**self.payload["travellers"][0], "name": "Other Person", "email": "other@example.com"}
        )
        trip = self.awaiting_admin()
        self.admin()
        self.post(f"/api/admin/requests/{trip.pk}/decision/", {"status": "approved"})
        trip.refresh_from_db()
        receipt = build_email(trip.notifications.get(event="applicant_receipt"), [])
        self.assertEqual(receipt["template_data"]["status"], "pending")
        self.assertEqual(receipt["template_data"]["destination"], trip.destination)
        self.assertEqual(len(receipt["template_data"]["travellers"]), 2)
        self.assertNotIn("review_url", receipt["template_data"])
        review = build_email(trip.notifications.get(event="supervisor_review"), [])
        self.assertIn(str(trip.approval_token), review["template_data"]["review_url"])
        for email, template, count in [
            ("jane@example.com", "applicant", 2),
            ("other@example.com", "traveller", 1),
            ("manager@example.com", "supervisor", 2),
        ]:
            notice = trip.notifications.get(event="admin_decision", recipient=email)
            result = build_email(notice, [])
            self.assertEqual(result["template"], f"travel/email/{template}_final_decision.html")
            self.assertEqual(len(result["template_data"]["travellers"]), count)
            self.assertNotIn(str(trip.approval_token), json.dumps(result))
            self.assertNotIn("admin_url", result["template_data"])

    @override_settings(NOTIFICATION_MODE="live")
    @patch("travel.email.transports.send_without_attachments")
    @patch("travel.email.transports.send_email_with_attachments")
    def test_transport_routes_per_actual_attachment_presence(self, attached, sqs):
        from .email.transports import dispatch_email

        email = {"event_id": "test", "recipient": "jane@example.com"}
        dispatch_email(email, [])
        sqs.assert_called_once_with(email)
        attached.assert_not_called()
        files = [
            {
                "filename": "ticket.pdf",
                "content_type": "application/pdf",
                "content": b"%PDF-",
            }
        ]
        dispatch_email(email, files)
        attached.assert_called_once_with(email, files)
        self.assertEqual(sqs.call_count, 1)

    @override_settings(
        NOTIFICATION_AWS_REGION="eu-west-1",
        NOTIFICATION_SQS_QUEUE_URL="",
        NOTIFICATION_SQS_QUEUE_NAME="notifications-email-queue",
    )
    @patch("travel.email.transports.boto3.client")
    def test_sqs_uses_configured_queue_and_serializes_template_model(self, factory):
        from .email.transports import send_without_attachments

        client = factory.return_value
        client.get_queue_url.return_value = {
            "QueueUrl": "https://sqs.eu-west-1.amazonaws.com/123456789012/notifications-email-queue"
        }
        email = {
            "schema_version": 1,
            "event_id": "travel-notification-1",
            "recipient": "jane@example.com",
            "template": "travel/email/applicant_receipt.html",
            "html_body": "<p>Nairobi &amp; Mombasa</p>",
            "text_body": "Nairobi & Mombasa",
        }
        send_without_attachments(email)
        client.get_queue_url.assert_called_once_with(QueueName="notifications-email-queue")
        args = client.send_message.call_args.kwargs
        self.assertEqual(args["QueueUrl"], client.get_queue_url.return_value["QueueUrl"])
        self.assertEqual(json.loads(args["MessageBody"]), email)
        self.assertNotIn("attachments", json.loads(args["MessageBody"]))

    @override_settings(
        NOTIFICATION_AWS_REGION="eu-west-1",
        NOTIFICATION_SQS_QUEUE_URL="https://sqs.eu-west-1.amazonaws.com/123456789012/notifications-email-queue",
    )
    @patch("travel.email.transports.boto3.client")
    def test_sqs_explicit_url_skips_lookup_and_failures_remain_retryable(self, factory):
        from .email.transports import send_without_attachments

        send_without_attachments({"event_id": "stable"})
        factory.return_value.get_queue_url.assert_not_called()
        trip = self.create()
        notice = trip.notifications.get(event="applicant_receipt")
        with override_settings(NOTIFICATION_MODE="live"):
            factory.return_value.send_message.side_effect = TimeoutError("private service details")
            deliver(notice.pk)
        notice.refresh_from_db()
        self.assertIsNone(notice.delivered_at)
        self.assertEqual(notice.last_error, "TimeoutError")
        self.assertEqual(notice.attempts, 1)

    @override_settings(NOTIFICATION_MODE="console", DEBUG=True)
    @patch("builtins.print")
    def test_console_mode_never_calls_aws_or_logs_private_review_link(self, log):
        trip = self.create()
        with patch("travel.email.transports.boto3.client") as client:
            deliver(trip.notifications.get(event="supervisor_review").pk)
        client.assert_not_called()
        self.assertNotIn(str(trip.approval_token), str(log.call_args))

    def test_django_email_rendering_all_stages_and_escaping(self):
        from .email.messages import build_email

        trip = self.create()
        trip.event_description = '<script>alert("unsafe")</script> & workshop'
        trip.save()
        scenarios = [
            ("applicant_receipt", "pending", "jane@example.com"),
            ("applicant_receipt", "awaiting_admin", "jane@example.com"),
            ("supervisor_review", "pending", "manager@example.com"),
            ("admin_review", "awaiting_admin", "desk@example.com"),
            ("decision", "awaiting_admin", "jane@example.com"),
            ("decision", "rejected", "jane@example.com"),
        ]
        self.payload["travellers"].append(
            {**self.payload["travellers"][0], "name": "Second Person", "email": "other@example.com"}
        )
        first = trip.travellers.first()
        AirTraveller.objects.create(
            request=trip,
            name="Second Person",
            email="other@example.com",
            departure_date=first.departure_date,
            departure_time=first.departure_time,
            return_date=first.return_date,
            return_time=first.return_time,
            ticket_class="economy",
        )
        for recipient in ["jane@example.com", "other@example.com", "manager@example.com"]:
            scenarios.extend(
                [("admin_decision", status, recipient) for status in ["approved", "rejected"]]
            )
        for event, status, recipient in scenarios:
            with self.subTest(event=event, status=status, recipient=recipient):
                notice = Notification(
                    pk=999,
                    request=trip,
                    event=event,
                    recipient=recipient,
                    payload={
                        "status": status,
                        "message": "<script>bad()</script> & instructions",
                        "reason": "Reschedule & discuss" if status == "rejected" else "",
                    },
                )
                attachments = (
                    [{"filename": "my-ticket.pdf"}]
                    if event == "admin_decision"
                    and status == "approved"
                    and recipient != "manager@example.com"
                    else []
                )
                result = build_email(notice, attachments)
                self.assertNotIn("template_data", result)
                self.assertTrue(result["template"].endswith(".html"))
                self.assertIn("&lt;script&gt;", result["html_body"])
                self.assertNotIn("<script>", result["html_body"])
                self.assertNotIn("{%", result["html_body"])
                self.assertIn("Destination: Mombasa, Kenya", result["text_body"])
                if event != "supervisor_review":
                    self.assertNotIn(str(trip.approval_token), result["html_body"])
                    self.assertNotIn(str(trip.approval_token), result["text_body"])
                if recipient == "other@example.com":
                    self.assertNotIn("Traveller â€” Jane Test", result["html_body"])
                    self.assertNotIn("TRAVELLER: Jane Test", result["text_body"])
                if attachments:
                    self.assertIn("my-ticket.pdf", result["html_body"])
                    self.assertIn("my-ticket.pdf", result["text_body"])
                if event == "decision" and status == "awaiting_admin":
                    self.assertIn("waiting for admin approval", result["text_body"])

    @patch("travel.notifications.send_notification_email")
    def test_direct_admin_approval_records_bypass_and_notifies_supervisor(self, send):
        from django.core.files.uploadedfile import SimpleUploadedFile

        trip = self.create()
        self.admin()
        person = trip.travellers.first()
        upload = self.client.post(
            f"/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/",
            {"ticket": SimpleUploadedFile("ticket.pdf", b"%PDF-1.7 synthetic")},
        )
        self.assertEqual(upload.status_code, 201)
        trip.refresh_from_db()
        self.assertEqual(trip.status, "pending")  # Upload must not advance the stage.
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(
                f"/api/admin/requests/{trip.pk}/decision/",
                {"status": "approved", "message": "Direct approval."},
            )
        self.assertEqual(response.status_code, 200)
        trip.refresh_from_db()
        self.assertEqual(trip.status, "approved")
        self.assertEqual(trip.supervisor_status, "bypassed")
        self.assertIsNone(trip.supervisor_decided_at)
        self.assertIsNotNone(trip.admin_decided_by_id)
        self.assertIsNotNone(trip.decided_at)
        calls = {c.args[0]: c for c in send.call_args_list}
        self.assertEqual(set(calls), {"jane@example.com", "manager@example.com"})
        self.assertEqual(calls["manager@example.com"].kwargs["attachments"], [])
        self.assertIn(
            "approved this request directly",
            calls["manager@example.com"].kwargs["email"]["html_body"],
        )
        self.assertEqual(len(calls["jane@example.com"].kwargs["attachments"]), 1)
        self.assertEqual(
            self.post(f"/api/review/{trip.approval_token}/", {"status": "rejected"}).status_code,
            409,
        )
        self.assertEqual(
            self.post(
                f"/api/admin/requests/{trip.pk}/decision/", {"status": "approved"}
            ).status_code,
            409,
        )
        self.assertEqual(trip.notifications.filter(event="admin_decision").count(), 2)
        self.assertEqual(
            self.client.get(f"/api/review/{trip.approval_token}/").json()["status"], "approved"
        )

    def test_admin_cannot_override_supervisor_rejection(self):
        trip = self.create()
        self.post(f"/api/review/{trip.approval_token}/", {"status": "rejected"})
        self.admin()
        self.assertEqual(
            self.post(
                f"/api/admin/requests/{trip.pk}/decision/", {"status": "approved"}
            ).status_code,
            409,
        )

    def test_public_form_token_recovers_after_admin_login_rotation(self):
        client = Client(enforce_csrf_checks=True)
        old_token = client.get("/api/session/").json()["csrf_token"]
        rotation_user = get_user_model().objects.create_user(
            username="rotation-admin", password="test-secret", is_staff=True
        )
        rotation_user.groups.add(Group.objects.get_or_create(name="Travel admins")[0])
        login_response = client.post(
            "/api/login/",
            json.dumps({"username": "rotation-admin", "password": "test-secret"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=old_token,
        )
        self.assertEqual(login_response.status_code, 200)
        failed = client.post(
            "/api/requests/",
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=old_token,
        )
        self.assertEqual(failed.status_code, 403)
        self.assertEqual(failed.json()["code"], "csrf_failed")
        self.assertNotIn("session has expired", failed.json()["error"])
        self.assertEqual(TravelRequest.objects.count(), 0)
        fresh_token = client.get("/api/session/").json()["csrf_token"]
        saved = client.post(
            "/api/requests/",
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=fresh_token,
        )
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(TravelRequest.objects.count(), 1)

    def test_public_form_recovers_missing_csrf_cookie_without_login(self):
        client = Client(enforce_csrf_checks=True)
        old_token = client.get("/api/session/").json()["csrf_token"]
        client.cookies.clear()
        failed = client.post(
            "/api/requests/",
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=old_token,
        )
        self.assertEqual(failed.json()["code"], "csrf_failed")
        fresh = client.get("/api/session/").json()
        self.assertFalse(fresh["is_admin"])
        saved = client.post(
            "/api/requests/",
            json.dumps(self.payload),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=fresh["csrf_token"],
        )
        self.assertEqual(saved.status_code, 201)

    @override_settings(NOTIFICATION_MODE="live", NOTIFICATION_USE_API_FOR_ALL_EMAILS=True)
    @patch("travel.email.transports.send_without_attachments")
    @patch("travel.email.transports.send_email_with_attachments")
    @patch("travel.email.transports.send_email_via_api")
    def test_api_override_sends_plain_emails_without_aws(self, api, tickets, sqs):
        from .email.transports import dispatch_email

        email = {"event_id": "test", "recipient": "jane@example.com"}
        dispatch_email(email, [])
        api.assert_called_once_with(email)
        tickets.assert_not_called()
        sqs.assert_not_called()
        files = [{"filename": "ticket.pdf"}]
        dispatch_email(email, files)
        tickets.assert_called_once_with(email, files)
        sqs.assert_not_called()

    @override_settings(
        NOTIFICATION_API_URL="https://internal.example.test/", NOTIFICATION_API_TOKEN=""
    )
    @patch("travel.email.transports.urlopen")
    def test_shared_api_adapter_matches_documented_endpoints(self, open_url):
        from .email.transports import send_email_via_api, send_email_with_attachments

        open_url.return_value.__enter__.return_value.status = 202
        email = {
            "event_id": "stable-123",
            "recipient": "jane@example.com",
            "subject": "Travel approved",
            "html_body": "<p>Hello</p>",
            "text_body": "Hello",
        }
        common = {"subject": email["subject"], "text": email["html_body"]}
        files = [
            {
                "filename": "ticket.pdf",
                "content_type": "application/pdf",
                "content": b"%PDF-",
            },
            {
                "filename": "document.png",
                "content_type": "image/png",
                "content": b"\x00\x7f\x80\xff",
            },
        ]
        mapped = [
            {
                "name": "ticket.pdf",
                "bytes": [37, 80, 68, 70, 45],
            },
            {"name": "document.png", "bytes": [0, 127, 128, 255]},
        ]
        cases = [
            ([], "sendEmail", {**common, "emailAddress": email["recipient"]}),
            (
                files[:1],
                "sendEmailMultiAttachment",
                {**common, "emailTo": email["recipient"], "attachments": mapped[:1]},
            ),
            (
                files,
                "sendEmailMultiAttachment",
                {**common, "emailTo": email["recipient"], "attachments": mapped},
            ),
        ]
        for attachments, endpoint, expected in cases:
            with self.subTest(endpoint=endpoint):
                if attachments:
                    send_email_with_attachments(email, attachments)
                else:
                    send_email_via_api(email)
                request = open_url.call_args.args[0]
                self.assertEqual(
                    request.full_url, f"https://internal.example.test/notification/email/{endpoint}"
                )
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.get_header("Content-type"), "application/json")
                self.assertIsNone(request.get_header("Authorization"))
                self.assertEqual(json.loads(request.data), expected)
                self.assertEqual(open_url.call_args.kwargs, {"timeout": 15})

    @override_settings(
        NOTIFICATION_API_URL="https://internal.example.test", NOTIFICATION_API_TOKEN="test-token"
    )
    @patch("travel.email.transports.urlopen")
    def test_api_adapter_propagates_rejection_and_supports_optional_gateway_token(self, open_url):
        from .email.transports import send_email_via_api

        email = {"recipient": "jane@example.com", "subject": "Travel", "html_body": "<p>Hello</p>"}
        open_url.return_value.__enter__.return_value.status = 503
        with self.assertRaises(RuntimeError):
            send_email_via_api(email)
        self.assertEqual(
            open_url.call_args.args[0].get_header("Authorization"), "Bearer test-token"
        )
        open_url.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            send_email_via_api(email)

    @override_settings(NOTIFICATION_MODE="live", NOTIFICATION_USE_API_FOR_ALL_EMAILS=True)
    @patch("travel.email.transports.send_email_via_api", side_effect=TimeoutError)
    def test_api_failure_keeps_saved_request_and_retryable_outbox(self, api):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post("/api/requests/", self.payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(TravelRequest.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(delivered_at__isnull=True, attempts=1).count(), 2
        )
        self.assertIn("queued", response.json()["message"])

    @patch("travel.notifications.send_notification_email")
    def test_admin_retries_one_email_and_does_not_resend_delivered(self, send):
        trip = self.create()
        notice = trip.notifications.get(event="applicant_receipt")
        Notification.objects.filter(pk=notice.pk).update(attempts=1, last_error="TimeoutError")
        url = f"/api/admin/requests/{trip.pk}/notifications/{notice.pk}/retry/"
        self.assertEqual(self.post(url, {}).status_code, 403)
        self.admin()
        self.assertEqual(self.client.get(url).status_code, 405)
        missing = f"/api/admin/requests/{uuid.uuid4()}/notifications/{notice.pk}/retry/"
        self.assertEqual(self.post(missing, {}).status_code, 404)
        self.assertEqual(self.post(url, {}).status_code, 200)
        notice.refresh_from_db()
        self.assertIsNotNone(notice.delivered_at)
        self.assertEqual(notice.attempts, 2)
        self.assertEqual(notice.last_error, "")
        self.assertIsNone(notice.delivery_started_at)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "jane@example.com")
        self.assertEqual(self.post(url, {}).status_code, 200)
        send.assert_called_once()
        self.assertIsNone(trip.notifications.get(event="supervisor_review").delivered_at)

    @patch("travel.notifications.send_notification_email", side_effect=TimeoutError)
    def test_admin_retry_failure_remains_retryable_and_csrf_protected(self, send):
        trip = self.create()
        notice = trip.notifications.get(event="applicant_receipt")
        self.admin()
        url = f"/api/admin/requests/{trip.pk}/notifications/{notice.pk}/retry/"
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(get_user_model().objects.get(username="admin"))
        self.assertEqual(self.post(url, {}, client=csrf_client).status_code, 403)
        self.assertEqual(self.post(url, {}).status_code, 502)
        notice.refresh_from_db()
        self.assertIsNone(notice.delivered_at)
        self.assertIsNone(notice.delivery_started_at)
        self.assertEqual(notice.last_error, "TimeoutError")
        self.assertEqual(notice.attempts, 1)

    @patch("travel.notifications.send_notification_email")
    def test_retry_claim_blocks_concurrent_send_and_recovers_stale_claim(self, send):
        trip = self.create()
        notice = trip.notifications.get(event="applicant_receipt")
        self.admin()
        url = f"/api/admin/requests/{trip.pk}/notifications/{notice.pk}/retry/"
        Notification.objects.filter(pk=notice.pk).update(delivery_started_at=timezone.now())
        self.assertEqual(self.post(url, {}).status_code, 409)
        send.assert_not_called()
        Notification.objects.filter(pk=notice.pk).update(
            delivery_started_at=timezone.now() - timedelta(minutes=11)
        )
        self.assertEqual(self.post(url, {}).status_code, 200)
        send.assert_called_once()

    @patch("travel.notifications.send_notification_email")
    def test_dns_failure_is_actionable_without_leaking_remote_details(self, send):
        import socket
        from urllib.error import URLError

        send.side_effect = URLError(socket.gaierror(11001, "sensitive remote details"))
        trip = self.create()
        notice = trip.notifications.get(event="applicant_receipt")
        self.admin()
        response = self.post(f"/api/admin/requests/{trip.pk}/notifications/{notice.pk}/retry/", {})
        self.assertEqual(response.status_code, 502)
        self.assertIn("DNS", response.json()["error"])
        self.assertNotIn("sensitive", response.json()["error"])
        notice.refresh_from_db()
        self.assertEqual(notice.last_error, "DNSResolutionError")
        self.assertIsNone(notice.delivered_at)
        detail = self.client.get(f"/api/admin/requests/{trip.pk}/").json()
        failed = next(n for n in detail["notifications"] if n["id"] == notice.pk)
        self.assertIn("VPN", failed["error_message"])

    def test_delivery_error_classification_keeps_only_safe_details(self):
        from urllib.error import HTTPError, URLError

        from .email.errors import delivery_error_code, delivery_error_message

        exc = HTTPError("https://secret-url", 503, "secret response", {}, None)
        self.assertEqual(delivery_error_code(exc), "HTTPError:503")
        self.assertIn("503", delivery_error_message(delivery_error_code(exc)))
        self.assertNotIn("secret", delivery_error_message(delivery_error_code(exc)))
        self.assertEqual(delivery_error_code(URLError(TimeoutError())), "TimeoutError")
        self.assertEqual(
            delivery_error_code(URLError(ConnectionRefusedError())), "ConnectionRefusedError"
        )

    @override_settings(NOTIFICATION_MODE="live", NOTIFICATION_USE_API_FOR_ALL_EMAILS=True)
    @patch("travel.email.transports.urlopen")
    def test_multiple_uploaded_documents_deliver_to_their_traveller_only(self, open_url):
        from django.core.files.uploadedfile import SimpleUploadedFile

        open_url.return_value.__enter__.return_value.status = 202
        self.payload["travellers"].append(
            {**self.payload["travellers"][0], "name": "Other", "email": "other@example.com"}
        )
        trip = self.create()
        self.admin()
        person = trip.travellers.get(email="jane@example.com")
        files = [
            ("Ticket.pdf", b"%PDF-1.7 ticket"),
            ("Supporting.pdf", b"%PDF-1.7 supporting\x00\x80\xff"),
        ]
        response = self.client.post(
            f"/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/",
            {
                "ticket": [
                    SimpleUploadedFile(name, data, content_type="application/pdf")
                    for name, data in files
                ]
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(response.json()["attachments"]), 2)
        with self.settings(NOTIFICATION_API_URL="https://internal.example.test"):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(
                    self.post(
                        f"/api/admin/requests/{trip.pk}/decision/", {"status": "approved"}
                    ).status_code,
                    200,
                )
        sent = {
            json.loads(c.args[0].data).get(
                "emailTo", json.loads(c.args[0].data).get("emailAddress")
            ): (
                c.args[0].full_url,
                json.loads(c.args[0].data),
            )
            for c in open_url.call_args_list
        }
        url, payload = sent["jane@example.com"]
        self.assertTrue(url.endswith("/sendEmailMultiAttachment"))
        self.assertEqual(
            payload["attachments"],
            [
                {
                    "name": name,
                    "bytes": list(data),
                }
                for name, data in files
            ],
        )
        self.assertNotIn("attachments", sent["other@example.com"][1])
        self.assertNotIn("attachments", sent["manager@example.com"][1])

    def test_multi_upload_validation_does_not_save_partial_selection(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import Ticket

        trip = self.create()
        self.admin()
        person = trip.travellers.first()
        response = self.client.post(
            f"/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/",
            {
                "ticket": [
                    SimpleUploadedFile("ticket.pdf", b"%PDF-1.7 ticket"),
                    SimpleUploadedFile("invalid.pdf", b"invalid content"),
                ]
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Ticket.objects.filter(traveller=person).exists())

    @patch("travel.notifications.send_notification_email")
    def test_undelivered_admin_list_and_retry_remove_only_successful_mail(self, send):
        trip = self.create()
        url = "/api/admin/notifications/"
        self.assertEqual(self.client.get(url).status_code, 403)
        self.admin()
        data = self.client.get(url).json()
        self.assertEqual(data["count"], 2)
        item = data["results"][0]
        self.assertEqual(item["request_id"], str(trip.pk))
        self.assertNotIn("payload", item)
        self.assertNotIn(str(trip.approval_token), json.dumps(data))
        retry_url = f"/api/admin/requests/{trip.pk}/notifications/{item['id']}/retry/"
        send.side_effect = TimeoutError
        self.assertEqual(self.post(retry_url, {}).status_code, 502)
        data = self.client.get(url).json()
        self.assertEqual(data["count"], 2)
        failed = next(n for n in data["results"] if n["id"] == item["id"])
        self.assertEqual(failed["attempts"], 1)
        self.assertIn("timed out", failed["error_message"])
        send.side_effect = None
        self.assertEqual(self.post(retry_url, {}).status_code, 200)
        data = self.client.get(url).json()
        self.assertEqual(data["count"], 1)
        self.assertNotIn(item["id"], [n["id"] for n in data["results"]])

    def test_undelivered_list_date_scope_and_pagination(self):
        trip = self.create()
        self.admin()
        TravelRequest.objects.filter(pk=trip.pk).update(
            created_at=timezone.now().replace(year=2024, month=3, day=10)
        )
        for i in range(21):
            Notification.objects.create(
                request=trip, event="applicant_receipt", recipient=f"person{i}@example.com"
            )
        self.assertEqual(
            self.client.get("/api/admin/notifications/?month=2024-03").json()["count"], 23
        )
        self.assertEqual(self.client.get("/api/admin/notifications/?year=2025").json()["count"], 0)
        self.assertEqual(self.client.get("/api/admin/notifications/?month=bad").status_code, 400)
        first = self.client.get("/api/admin/notifications/?year=2024").json()
        second = self.client.get("/api/admin/notifications/?year=2024&page=2").json()
        self.assertEqual(len(first["results"]), 20)
        self.assertEqual(len(second["results"]), 3)
        self.assertEqual(
            first["results"][0]["id"], trip.notifications.order_by("-created_at", "-id").first().id
        )

    def test_organisation_identity_validation_and_department_search(self):
        Department.objects.create(name="Finance")
        Department.objects.create(name="Former department", active=False)
        result = self.client.get("/api/departments/?search=fin").json()["results"]
        self.assertEqual([r["name"] for r in result], ["Finance"])
        self.assertNotIn(
            "Former department",
            [r["name"] for r in self.client.get("/api/departments/").json()["results"]],
        )
        for key, value in [("company", "Unknown"), ("department_id", 999999)]:
            payload = {**self.payload, key: value}
            self.assertEqual(self.post("/api/requests/", payload).status_code, 400)
        for key, value in [
            ("document_type", "invalid"),
            ("document_number", ""),
            ("checked_luggage", "yes"),
        ]:
            payload = copy.deepcopy(self.payload)
            payload["travellers"][0][key] = value
            self.assertEqual(self.post("/api/requests/", payload).status_code, 400)
        trip = self.create()
        detail = self.client.get(f"/api/review/{trip.approval_token}/").json()
        self.assertEqual(detail["company"], "Apa General")
        self.assertEqual(detail["department"], "Operations")
        self.assertEqual(detail["travellers"][0]["document_number"], "TEST1234")
        self.assertTrue(detail["travellers"][0]["checked_luggage"])
        self.admin()
        data = self.client.get("/api/admin/analytics/?company=Apa%20General").json()
        self.assertEqual(data["companies"], [{"company": "Apa General", "requests": 1}])
        self.assertEqual(data["departments"], [{"department__name": "Operations", "requests": 1}])
        self.assertEqual(
            self.client.get("/api/admin/analytics/?company=APA%20life").json()["filtered_total"], 0
        )

    @override_settings(TRAVEL_S3_BUCKET="test-private", TRAVEL_S3_REGION="eu-west-1")
    @patch("travel.supporting_documents.boto3.client")
    def test_supporting_documents_private_s3_upload_and_access(self, boto):
        from django.core.files.uploadedfile import SimpleUploadedFile

        storage = boto.return_value

        def files():
            return [
                SimpleUploadedFile("memo.pdf", b"%PDF-1.7 memo"),
                SimpleUploadedFile(
                    "approval.eml",
                    b"From: supervisor@example.com\r\nSubject: Approval\r\n\r\nApproved.",
                ),
            ]

        response = self.client.post(
            "/api/requests/", {"payload": json.dumps(self.payload), "supporting_documents": files()}
        )
        self.assertEqual(response.status_code, 201, response.content)
        trip = TravelRequest.objects.get()
        self.assertEqual(trip.supporting_documents.count(), 2)
        self.assertEqual(storage.put_object.call_count, 2)
        self.assertEqual(storage.put_object.call_args.kwargs["ServerSideEncryption"], "AES256")
        self.assertNotIn("ACL", storage.put_object.call_args.kwargs)
        self.assertEqual(
            self.client.post(
                "/api/requests/",
                {"payload": json.dumps(self.payload), "supporting_documents": files()},
            ).status_code,
            200,
        )
        self.assertEqual(storage.put_object.call_count, 2)
        document = trip.supporting_documents.first()
        url = f"/api/admin/requests/{trip.pk}/documents/{document.pk}/"
        self.assertEqual(self.client.get(url).status_code, 403)
        wrong = f"/api/review/{uuid.uuid4()}/requests/{trip.pk}/documents/{document.pk}/"
        self.assertEqual(self.client.get(wrong).status_code, 403)
        storage.generate_presigned_url.return_value = "https://s3.example.test/private-download"
        correct = f"/api/review/{trip.approval_token}/requests/{trip.pk}/documents/{document.pk}/"
        self.assertEqual(self.client.get(correct).status_code, 302)
        self.assertEqual(storage.generate_presigned_url.call_args.kwargs["ExpiresIn"], 300)
        self.admin()
        self.assertEqual(self.client.get(url).status_code, 302)
        detail = self.client.get(f"/api/admin/requests/{trip.pk}/").json()
        self.assertNotIn(document.object_key, json.dumps(detail))

    @override_settings(TRAVEL_S3_BUCKET="test-private", TRAVEL_S3_REGION="eu-west-1")
    @patch("travel.supporting_documents.boto3.client")
    def test_supporting_upload_failure_rolls_back_and_cleans_up(self, boto):
        from django.core.files.uploadedfile import SimpleUploadedFile

        storage = boto.return_value
        storage.put_object.side_effect = [None, TimeoutError()]
        result = self.client.post(
            "/api/requests/",
            {
                "payload": json.dumps(self.payload),
                "supporting_documents": [
                    SimpleUploadedFile("memo.pdf", b"%PDF-1.7 memo"),
                    SimpleUploadedFile("approval.pdf", b"%PDF-1.7 approval"),
                ],
            },
        )
        self.assertEqual(result.status_code, 503)
        self.assertFalse(TravelRequest.objects.exists())
        self.assertFalse(SupportingDocument.objects.exists())
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(storage.delete_object.call_count, 2)

    @patch("travel.supporting_documents.boto3.client")
    def test_supporting_invalid_file_rejected_before_storage(self, boto):
        from django.core.files.uploadedfile import SimpleUploadedFile

        result = self.client.post(
            "/api/requests/",
            {
                "payload": json.dumps(self.payload),
                "supporting_documents": SimpleUploadedFile("bad.pdf", b"not a PDF"),
            },
        )
        self.assertEqual(result.status_code, 400)
        self.assertFalse(TravelRequest.objects.exists())
        boto.assert_not_called()

    @override_settings(TRAVEL_S3_BUCKET="test-private", TRAVEL_S3_REGION="eu-west-1")
    @patch("travel.supporting_documents.boto3.client")
    def test_admin_submission_accepts_shared_supporting_document(self, boto):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.admin()
        payload = {**self.payload, "supervisor_email": ""}
        result = self.client.post(
            "/api/admin/requests/new/",
            {
                "payload": json.dumps(payload),
                "supporting_documents": SimpleUploadedFile("memo.pdf", b"%PDF-1.7 memo"),
            },
        )
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.json()["supervisor_status"], "not_required")
        self.assertEqual(len(result.json()["supporting_documents"]), 1)

    @patch("travel.notifications.send_notification_email")
    def test_late_tickets_partial_delivery_retry_and_additional_batch(self, send):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.payload["travellers"].append(
            {**self.payload["travellers"][0], "name": "Other", "email": "other@example.com"}
        )
        trip = self.create()
        self.admin()
        self.post(f"/api/admin/requests/{trip.pk}/decision/", {"status": "approved"})
        url = f"/api/admin/requests/{trip.pk}/send-tickets/"

        def pending():
            return self.client.get("/api/admin/metrics/").json()["awaiting_tickets"]

        self.assertEqual(pending(), 1)
        self.assertEqual(
            self.client.get("/api/admin/requests/?status=awaiting_tickets").json()["count"], 1
        )
        people = list(trip.travellers.all())

        def upload(person, name):
            response = self.client.post(
                f"/api/admin/requests/{trip.pk}/travellers/{person.pk}/tickets/",
                {"ticket": SimpleUploadedFile(name, b"%PDF-1.7 ticket")},
            )
            self.assertEqual(response.status_code, 201)
            return response.json()["id"]

        first = upload(people[0], "first.pdf")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(
                self.post(
                    url, {"traveller_messages": {str(people[0].pk): "Your instructions"}}
                ).status_code,
                200,
            )
        self.assertEqual(send.call_args.args[0], people[0].email)
        self.assertEqual(len(send.call_args.kwargs["attachments"]), 1)
        self.assertEqual(send.call_args.kwargs["event"], "tickets")
        self.assertEqual(pending(), 1)
        self.assertEqual(self.post(url, {}).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/admin/tickets/{first}/").status_code, 409)
        upload(people[1], "second.pdf")
        send.side_effect = TimeoutError
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post(url, {}).status_code, 200)
        self.assertEqual(pending(), 1)
        failed = trip.notifications.get(event="tickets", recipient=people[1].email)
        self.assertIsNone(failed.delivered_at)
        send.side_effect = None
        self.assertEqual(
            self.post(
                f"/api/admin/requests/{trip.pk}/notifications/{failed.pk}/retry/", {}
            ).status_code,
            200,
        )
        self.assertEqual(pending(), 0)
        upload(people[0], "extra.pdf")
        self.assertEqual(pending(), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.post(url, {}).status_code, 200)
        self.assertEqual(
            [a["filename"] for a in send.call_args.kwargs["attachments"]], ["extra.pdf"]
        )
        self.assertEqual(pending(), 0)
        trip.refresh_from_db()
        self.assertEqual(trip.status, "approved")
        detail = self.client.get(f"/api/admin/requests/{trip.pk}/").json()
        self.assertTrue(all(p["tickets_sent"] for p in detail["travellers"]))

    def test_send_tickets_requires_staff_and_approval(self):
        trip = self.create()
        url = f"/api/admin/requests/{trip.pk}/send-tickets/"
        self.assertEqual(self.post(url, {}).status_code, 403)
        self.admin()
        self.assertEqual(self.post(url, {}).status_code, 409)
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_ticket_email_template_is_private_and_distinct_from_approval(self):
        from .email.messages import build_email

        trip = self.create()
        notice = Notification.objects.create(
            request=trip,
            event="tickets",
            recipient=trip.applicant_email,
            payload={"status": "approved", "recipient_role": "traveller"},
        )
        email = build_email(notice, [{"filename": "ticket.pdf"}])
        self.assertIn("Your travel tickets", email["subject"])
        self.assertNotIn(str(trip.approval_token), email["html_body"])
        self.assertIn("ticket.pdf", email["html_body"])

    def test_historical_ticket_delivery_backfill(self):
        import importlib

        from django.apps import apps

        from .models import Ticket

        trip = self.create()
        TravelRequest.objects.filter(pk=trip.pk).update(status="approved")
        ticket = Ticket.objects.create(
            traveller=trip.travellers.first(),
            filename="old.pdf",
            content_type="application/pdf",
            content=b"%PDF-",
            size=5,
        )
        notice = Notification.objects.create(
            request=trip,
            event="admin_decision",
            recipient=trip.applicant_email,
            payload={"status": "approved", "ticket_ids": [ticket.pk]},
            delivered_at=timezone.now(),
        )
        importlib.import_module(
            "travel.migrations.0006_backfill_ticket_notifications"
        ).link_existing(apps, None)
        ticket.refresh_from_db()
        self.assertEqual(ticket.notification_id, notice.pk)
        self.admin()
        self.assertEqual(self.client.get("/api/admin/metrics/").json()["awaiting_tickets"], 0)

    def test_combined_analytics_filters_scope_all_cards_and_charts(self):
        self.analytics_fixture()
        finance = Department.objects.create(name="Finance analytics")
        TravelRequest.objects.filter(status="approved").update(
            company="APA life", department=finance
        )
        department = self.payload["department_id"]
        cases = [
            (
                {
                    "year": 2025,
                    "status": "pending",
                    "company": "Apa General",
                    "department": department,
                },
                1,
                2,
            ),
            (
                {
                    "year": 2025,
                    "status": "approved",
                    "company": "APA life",
                    "department": finance.pk,
                },
                1,
                1,
            ),
            (
                {
                    "year": 2025,
                    "status": "approved",
                    "company": "Apa General",
                    "department": department,
                },
                0,
                0,
            ),
            (
                {
                    "year": 2025,
                    "status": "pending",
                    "company": "APA life",
                    "department": finance.pk,
                },
                0,
                0,
            ),
            (
                {
                    "year": 2025,
                    "status": "pending",
                    "email": "jane@example.com",
                    "department": department,
                },
                1,
                2,
            ),
            ({"year": 2025, "status": "approved", "email": "jane@example.com"}, 0, 0),
        ]
        for filters, total, travellers in cases:
            with self.subTest(filters=filters):
                data = self.client.get("/api/admin/analytics/", filters).json()
                self.assertEqual(data["total"], total)
                self.assertEqual(data["filtered_total"], total)
                self.assertEqual(data["travellers"], travellers)
                self.assertEqual(sum(data["counts"].values()), total)
                self.assertEqual(data["counts"][filters["status"]], total)
                for key in [
                    "monthly",
                    "travel_types",
                    "companies",
                    "departments",
                    "top_destinations",
                ]:
                    self.assertEqual(sum(row["requests"] for row in data[key]), total)

    def test_ticket_cost_validation_permissions_and_clearing(self):
        from .models import Ticket

        trip = self.create()
        ticket = Ticket.objects.create(
            traveller=trip.travellers.first(),
            filename="ticket.pdf",
            content_type="application/pdf",
            content=b"%PDF-",
            size=5,
        )
        url = f"/api/admin/tickets/{ticket.pk}/"
        self.assertEqual(
            self.client.patch(
                url, json.dumps({"cost": "100.10"}), content_type="application/json"
            ).status_code,
            403,
        )
        self.admin()
        for invalid in ["-1", "NaN", "Infinity", "1.001", "10000000000", "invalid", True]:
            self.assertEqual(
                self.client.patch(
                    url, json.dumps({"cost": invalid}), content_type="application/json"
                ).status_code,
                400,
            )
        for valid in ["100.10", "0", None]:
            response = self.client.patch(
                url, json.dumps({"cost": valid}), content_type="application/json"
            )
            self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertIsNone(ticket.cost)

    def test_cross_year_month_range_and_cost_aggregates(self):
        from decimal import Decimal

        from .models import Ticket

        self.analytics_fixture()
        trips = list(TravelRequest.objects.order_by("created_at"))
        for trip, amount in zip(trips, ["100.10", "200.20", "300.30"]):
            Ticket.objects.create(
                traveller=trip.travellers.first(),
                filename="ticket.pdf",
                content_type="application/pdf",
                content=b"%PDF-",
                size=5,
                cost=amount,
            )
        # Extra unpriced files must not affect spending or request counts.
        Ticket.objects.create(
            traveller=trips[1].travellers.first(),
            filename="memo.pdf",
            content_type="application/pdf",
            content=b"%PDF-",
            size=5,
        )
        data = self.client.get("/api/admin/analytics/?from_month=2025-12&to_month=2026-01").json()
        self.assertEqual(data["total"], 2)
        self.assertEqual([m["label"] for m in data["monthly"]], ["Dec 2025", "Jan 2026"])
        self.assertEqual([m["requests"] for m in data["monthly"]], [1, 1])
        self.assertEqual(Decimal(data["total_cost"]), Decimal("500.50"))
        self.assertEqual(data["costed_tickets"], 2)
        for key in ["cost_by_company", "cost_by_department"]:
            self.assertEqual(sum(Decimal(row["amount"]) for row in data[key]), Decimal("500.50"))
        month = self.client.get("/api/admin/analytics/?month=2025-12&status=approved").json()
        self.assertEqual(month["total"], 1)
        self.assertEqual(len(month["monthly"]), 1)
        self.assertEqual(Decimal(month["total_cost"]), Decimal("200.20"))
        empty = self.client.get("/api/admin/analytics/?month=2025-12&status=pending").json()
        self.assertEqual(Decimal(empty["total_cost"]), 0)
        for query in [
            "from_month=2026-01&to_month=2025-12",
            "from_month=2025-01",
            "month=2025-13",
            "month=2025-01&from_month=2025-01&to_month=2025-02",
        ]:
            self.assertEqual(self.client.get("/api/admin/analytics/?" + query).status_code, 400)

    def test_roles_separate_travel_portal_from_developer_admin(self):
        from .user_forms import PortalUserCreationForm

        developer = get_user_model().objects.create_user(
            username="developer", password="test-secret", is_staff=True
        )
        developer.groups.add(Group.objects.get_or_create(name="Developers")[0])
        self.client.force_login(developer)
        self.assertEqual(self.client.get("/api/admin/metrics/").status_code, 403)
        self.assertFalse(self.client.get("/api/session/").json()["is_admin"])
        self.assertEqual(self.client.get("/django-admin/auth/user/").status_code, 200)
        self.assertEqual(self.client.get("/django-admin/travel/department/add/").status_code, 200)
        form = PortalUserCreationForm(
            data={
                "username": "travel-desk",
                "email": "Desk@example.com",
                "first_name": "Travel",
                "last_name": "Desk",
                "role": "Travel admins",
                "password1": "Strong-Test-Password-451!",
                "password2": "Strong-Test-Password-451!",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertFalse(user.is_staff)
        self.assertTrue(user.groups.filter(name="Travel admins").exists())
        self.client.logout()
        response = self.post(
            "/api/login/", {"username": "travel-desk", "password": "Strong-Test-Password-451!"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/admin/metrics/").status_code, 200)
        self.assertEqual(self.client.get("/django-admin/auth/user/").status_code, 302)
        user.is_active = False
        user.save()
        self.assertEqual(self.client.get("/api/admin/metrics/").status_code, 403)

    @override_settings(TRAVEL_ADMIN_EMAILS=["ignored@example.com"])
    def test_notification_recipients_come_only_from_active_travel_users(self):
        from .notifications import queue_admin_review

        trip = self.create()
        for name, role, active, email in [
            ("active", "Travel admins", True, "desk@example.com"),
            ("duplicate", "Travel admins", True, "DESK@example.com"),
            ("inactive", "Travel admins", False, "inactive@example.com"),
            ("developer", "Developers", True, "developer@example.com"),
        ]:
            user = get_user_model().objects.create_user(
                username=name, email=email, is_active=active
            )
            user.groups.add(Group.objects.get_or_create(name=role)[0])
        queue_admin_review(trip)
        self.assertEqual(
            list(
                trip.notifications.filter(event="admin_review").values_list("recipient", flat=True)
            ),
            ["desk@example.com"],
        )

    def test_developer_form_and_legacy_account_role_migration(self):
        import importlib

        from django.apps import apps

        from .user_forms import PortalUserCreationForm

        old = get_user_model().objects.create_user(
            username="legacy", password="legacy-password", is_staff=True
        )
        root = get_user_model().objects.create_superuser(
            username="root", password="root-password", email="root@example.com"
        )
        original_hash = old.password
        importlib.import_module("travel.migrations.0008_user_roles").assign_roles(apps, None)
        old.refresh_from_db()
        self.assertEqual(old.password, original_hash)
        self.assertFalse(old.is_staff)
        self.assertTrue(old.groups.filter(name="Travel admins").exists())
        self.assertTrue(root.groups.filter(name="Developers").exists())
        form = PortalUserCreationForm(
            data={
                "username": "new-developer",
                "email": "dev@example.com",
                "role": "Developers",
                "password1": "Strong-Test-Password-451!",
                "password2": "Strong-Test-Password-451!",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        developer = form.save()
        self.assertTrue(developer.is_staff)
        self.assertTrue(developer.groups.filter(name="Developers").exists())

    def test_developer_creates_and_changes_user_role_in_django_admin(self):
        developer = get_user_model().objects.create_user(
            username="dev-manager", password="test-secret", is_staff=True
        )
        developer.groups.add(Group.objects.get_or_create(name="Developers")[0])
        self.client.force_login(developer)
        response = self.client.post(
            "/django-admin/auth/user/add/",
            {
                "username": "managed-admin",
                "email": "managed@example.com",
                "first_name": "Managed",
                "last_name": "Admin",
                "role": "Travel admins",
                "password1": "Strong-Travel-Pass-887!",
                "password2": "Strong-Travel-Pass-887!",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:1000])
        user = get_user_model().objects.get(username="managed-admin")
        self.assertTrue(user.check_password("Strong-Travel-Pass-887!"))
        self.assertFalse(user.is_staff)
        self.assertTrue(user.groups.filter(name="Travel admins").exists())
        response = self.client.post(
            f"/django-admin/auth/user/{user.pk}/change/",
            {
                "username": user.username,
                "email": user.email,
                "first_name": "Managed",
                "last_name": "Developer",
                "role": "Developers",
                "is_active": "on",
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 302, response.content[:1000])
        user.refresh_from_db()
        self.assertTrue(user.is_staff)
        self.assertTrue(user.groups.filter(name="Developers").exists())
        self.assertFalse(user.groups.filter(name="Travel admins").exists())

    def test_portal_admin_user_management_and_role_boundary(self):
        payload = {
            "username": "new-travel-admin",
            "email": "new@example.com",
            "first_name": "New",
            "last_name": "Admin",
            "password": "Strong-Password-55!",
            "active": True,
        }
        self.assertEqual(self.post("/api/admin/users/", payload).status_code, 403)
        self.admin()
        self.assertEqual(
            self.post("/api/admin/users/", {**payload, "role": "Developers"}).status_code, 403
        )
        self.assertEqual(
            self.post("/api/admin/users/", {**payload, "password": "short"}).status_code, 400
        )
        result = self.post("/api/admin/users/", payload)
        self.assertEqual(result.status_code, 201, result.content)
        user = get_user_model().objects.get(username=payload["username"])
        self.assertTrue(user.check_password(payload["password"]))
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.groups.filter(name="Travel admins").exists())
        self.assertNotIn("password", result.json())
        self.assertEqual(self.post("/api/admin/users/", payload).status_code, 400)
        update = {
            "email": user.email,
            "first_name": "Updated",
            "last_name": "Admin",
            "active": False,
        }
        response = self.client.patch(
            f"/api/admin/users/{user.pk}/", json.dumps(update), content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        myself = get_user_model().objects.get(username="admin")
        self.assertEqual(
            self.client.patch(
                f"/api/admin/users/{myself.pk}/",
                json.dumps(update),
                content_type="application/json",
            ).status_code,
            400,
        )
        developer = get_user_model().objects.create_superuser(
            username="protected-developer", email="dev@example.com", password="test-password"
        )
        self.assertEqual(
            self.client.patch(
                f"/api/admin/users/{developer.pk}/",
                json.dumps(update),
                content_type="application/json",
            ).status_code,
            404,
        )
        self.assertNotIn(
            developer.pk, [u["id"] for u in self.client.get("/api/admin/users/").json()["results"]]
        )
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(myself)
        self.assertEqual(self.post("/api/admin/users/", payload, client=secure).status_code, 403)

    def test_portal_department_configuration_and_public_picker(self):
        self.assertEqual(self.client.get("/api/admin/departments/").status_code, 403)
        self.admin()
        result = self.post("/api/admin/departments/", {"name": "Legal", "active": True})
        self.assertEqual(result.status_code, 201)
        pk = result.json()["id"]
        self.assertEqual(self.post("/api/admin/departments/", {"name": "legal"}).status_code, 400)
        self.assertEqual(
            self.client.get("/api/departments/?search=Legal").json()["results"][0]["id"], pk
        )
        result = self.client.patch(
            f"/api/admin/departments/{pk}/",
            json.dumps({"name": "Legal affairs", "active": False}),
            content_type="application/json",
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.client.get("/api/departments/?search=Legal").json()["results"], [])
        listed = self.client.get("/api/admin/departments/?search=Legal").json()
        self.assertEqual(listed["count"], 1)
        self.assertFalse(listed["results"][0]["active"])


class DeveloperPortalDeliveryTests(TestCase):
    @override_settings(
        DEBUG=False, ALLOWED_HOSTS=["testserver"], CSRF_TRUSTED_ORIGINS=["http://127.0.0.1:5173"]
    )
    def test_admin_login_accepts_valid_csrf_and_rejects_missing_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.get("/django-admin/login/")
        self.assertEqual(response.status_code, 200)
        token = client.cookies["csrftoken"].value
        payload = {"username": "nonexistent-test-user", "password": "invalid-password"}
        response = client.post("/django-admin/login/", payload, HTTP_ORIGIN="http://127.0.0.1:5173")
        self.assertEqual(response.status_code, 403)
        response = client.post(
            "/django-admin/login/",
            {**payload, "csrfmiddlewaretoken": token},
            HTTP_ORIGIN="http://127.0.0.1:5173",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please enter the correct")

    def test_admin_styles_served_with_debug_disabled(self):
        import shutil
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from django.contrib.staticfiles import finders

        with TemporaryDirectory() as directory:
            destination = Path(directory) / "admin/css/base.css"
            destination.parent.mkdir(parents=True)
            shutil.copyfile(finders.find("admin/css/base.css"), destination)
            with override_settings(
                DEBUG=False, STATIC_ROOT=directory, ALLOWED_HOSTS=["testserver"]
            ):
                response = Client().get("/static/admin/css/base.css")
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/css", response["Content-Type"])
                response.close()
