import copy
import json
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from .models import TravelRequest, AirTraveller, Notification
from .notifications import deliver


@override_settings(NOTIFICATION_MODE='console')
class TravelWorkflowTests(TestCase):
    def test_health_is_public_and_not_cached(self):
        response = self.client.get('/api/health/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
        self.assertEqual(response['Cache-Control'], 'no-store')

    @patch('travel.views.TravelRequest.objects.exists', side_effect=DatabaseError('private database detail'))
    def test_health_reports_database_unavailable_without_details(self, exists):
        response = self.client.get('/api/health/')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})

    def setUp(self):
        departure = timezone.localdate() + timedelta(days=10)
        self.payload = {'submission_key': str(uuid.uuid4()), 'destination': 'Mombasa, Kenya',
                        'event_description': 'Regional planning meeting', 'applicant_name': 'Jane Test',
                        'applicant_email': 'jane@example.com', 'supervisor_email': 'manager@example.com',
                        'travellers': [{'name': 'Jane Test', 'email': 'jane@example.com',
                                        'departure_date': departure.isoformat(), 'departure_time': '09:00',
                                        'return_date': (departure + timedelta(days=2)).isoformat(),
                                        'return_time': '17:00', 'ticket_class': 'economy'}]}

    def post(self, url, payload, client=None):
        return (client or self.client).post(url, json.dumps(payload), content_type='application/json')

    def create(self):
        response = self.post('/api/requests/', self.payload)
        self.assertEqual(response.status_code, 201, response.content)
        return TravelRequest.objects.get()

    def admin(self):
        user = get_user_model().objects.create_user(username='admin', password='test-secret', is_staff=True)
        self.client.force_login(user)

    @patch('travel.notifications.send_notification_email')
    def test_submission_sends_two_distinct_emails_and_hides_credential(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post('/api/requests/', self.payload)
        trip = TravelRequest.objects.get()
        self.assertEqual(send.call_count, 2)
        supervisor, applicant = send.call_args_list
        self.assertEqual(supervisor.args[0], 'manager@example.com')
        self.assertIn(str(trip.approval_token), supervisor.args[3])
        self.assertIsNone(applicant.args[3])
        self.assertEqual(applicant.args[0], 'jane@example.com')
        self.assertNotIn(str(trip.approval_token), response.content.decode())
        self.assertNotEqual(trip.id, trip.approval_token)
        self.assertEqual(Notification.objects.filter(delivered_at__isnull=False).count(), 2)

    def test_retried_submission_is_idempotent(self):
        self.create()
        response = self.post('/api/requests/', self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TravelRequest.objects.count(), 1)
        self.assertEqual(Notification.objects.count(), 2)

    @patch('travel.notifications.send_notification_email')
    def test_approval_and_duplicate_decision(self, send):
        trip = self.create()
        url = f'/api/review/{trip.approval_token}/'
        self.assertEqual(self.client.get(url).json()['status'], 'pending')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(url, {'status': 'approved'})
        self.assertEqual(response.json()['status'], 'awaiting_admin')
        self.assertEqual(send.call_args.kwargs['status'], 'awaiting_admin')
        self.assertEqual(send.call_args.args[0], 'jane@example.com')
        self.assertIsNone(send.call_args.args[3])
        self.assertEqual(self.post(url, {'status': 'rejected'}).status_code, 409)
        self.assertEqual(Notification.objects.filter(event='decision').count(), 1)
        trip.refresh_from_db()
        self.assertEqual(trip.status, 'awaiting_admin')
        self.assertIsNone(trip.decided_at)
        self.assertEqual(trip.supervisor_status, "approved")
        self.assertIsNotNone(trip.supervisor_decided_at)

    def test_reject_with_and_without_reason(self):
        trip = self.create()
        response = self.post(f'/api/review/{trip.approval_token}/', {'status': 'rejected', 'reason': 'Please reschedule.'})
        self.assertEqual(response.json()['rejection_reason'], 'Please reschedule.')
        self.payload['submission_key'] = str(uuid.uuid4())
        self.post('/api/requests/', self.payload)
        other = TravelRequest.objects.exclude(pk=trip.pk).get()
        self.assertEqual(self.post(f'/api/review/{other.approval_token}/', {'status': 'rejected'}).status_code, 200)

    def test_reference_does_not_grant_review_access(self):
        trip = self.create()
        self.assertEqual(self.client.get(f'/api/review/{trip.id}/').status_code, 404)
        self.assertEqual(self.client.get(f'/api/review/{uuid.uuid4()}/').status_code, 404)

    def test_validation(self):
        cases = [({'supervisor_email': 'not-an-email'}, 'email'),
                 ({'supervisor_email': 'JANE@example.com'}, 'differ'),
                 ({'travellers': []}, 'travellers'), ({'travel_type': 'local'}, 'air'),
                 ({'destination': ''}, 'Destination'), ({'travellers': [None]}, 'Traveller'),
                 ({'submission_key': 'bad'}, 'key')]
        for changes, message in cases:
            with self.subTest(changes=changes):
                response = self.post('/api/requests/', {**self.payload, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()['error'])
        self.assertEqual(TravelRequest.objects.count(), 0)

    def test_date_validation_and_short_notice(self):
        data = copy.deepcopy(self.payload)
        data['travellers'][0]['return_date'] = data['travellers'][0]['departure_date']
        data['travellers'][0]['return_time'] = '08:00'
        self.assertEqual(self.post('/api/requests/', data).status_code, 400)
        data = copy.deepcopy(self.payload)
        data['travellers'][0]['departure_date'] = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertEqual(self.post('/api/requests/', data).status_code, 400)
        data['travellers'][0]['departure_date'] = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.post('/api/requests/', data).status_code, 201)

    def test_admin_access_and_logout(self):
        self.create()
        for url in ['/api/admin/requests/', '/api/admin/metrics/', '/api/admin/people/?email=jane@example.com']:
            self.assertEqual(self.client.get(url).status_code, 403)
        user = get_user_model().objects.create_user(username='employee', password='test-secret')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/api/admin/requests/').status_code, 403)
        self.assertEqual(self.post('/api/login/', {'username': 'employee', 'password': 'test-secret'}).status_code, 401)
        self.admin()
        response = self.client.get('/api/admin/requests/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('approval_token', response.content.decode())
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.post('/api/logout/', {})
        self.assertEqual(self.client.get('/api/admin/metrics/').status_code, 403)

    def test_metrics_filter_order_and_person_history(self):
        first = self.create()
        self.payload['submission_key'] = str(uuid.uuid4())
        self.payload['applicant_email'] = 'other@example.com'
        self.payload['destination'] = 'Kisumu'
        self.post('/api/requests/', self.payload)
        self.admin()
        rows = self.client.get('/api/admin/requests/').json()
        self.assertEqual(rows['count'], 2)
        self.assertEqual(rows['results'][0]['destination'], 'Kisumu')
        self.assertEqual(self.client.get('/api/admin/requests/?search=Mombasa').json()['count'], 1)
        self.assertEqual(self.client.get('/api/admin/requests/?status=approved').json()['count'], 0)
        metrics = self.client.get('/api/admin/metrics/').json()
        self.assertEqual(metrics['counts']['pending'], 2)
        self.assertEqual(metrics['travellers'], 2)
        self.assertEqual(sum(p['requests'] for p in metrics['daily']), 2)
        history = self.client.get('/api/admin/people/?email=JANE@example.com').json()
        self.assertEqual(history['total'], 2)  # applicant + traveller deduplicated
        detail = self.client.get(f'/api/admin/requests/{first.id}/').json()
        self.assertEqual(detail['travellers'][0]['email'], 'jane@example.com')
        self.assertNotIn('approval_token', detail)

    @patch('travel.notifications.send_notification_email', side_effect=TimeoutError)
    def test_email_failure_keeps_request_and_supports_retry(self, send):
        with self.captureOnCommitCallbacks(execute=True):
            trip = self.create()
        self.assertEqual(trip.status, 'pending')
        notice = Notification.objects.get(event='supervisor_review')
        self.assertIsNone(notice.delivered_at)
        self.assertEqual(notice.attempts, 1)
        send.side_effect = None
        deliver(notice.pk)
        notice.refresh_from_db()
        self.assertIsNotNone(notice.delivered_at)
        self.assertEqual(notice.attempts, 2)
        self.assertEqual(send.call_args.kwargs['idempotency_key'], f'travel-notification-{notice.pk}')
        calls = send.call_count
        deliver(notice.pk)
        self.assertEqual(send.call_count, calls)

    def test_csrf_is_enforced_on_public_and_admin_posts(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post('/api/requests/', self.payload, client).status_code, 403)
        token = client.get('/api/session/').json()['csrf_token']
        response = client.post('/api/requests/', json.dumps(self.payload), content_type='application/json', HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 201)
        trip = TravelRequest.objects.get()
        self.assertEqual(self.post(f'/api/review/{trip.approval_token}/', {'status': 'approved'}, client).status_code, 403)
        self.assertEqual(self.post('/api/login/', {'username': 'x', 'password': 'y'}, client).status_code, 403)

    def test_malformed_json(self):
        self.assertEqual(self.client.post('/api/requests/', '{', content_type='application/json').status_code, 400)
        self.assertEqual(self.post('/api/requests/', []).status_code, 400)

    def analytics_fixture(self):
        trip = self.create()
        # Duplicate traveller rows must not inflate a person's request counts.
        person = trip.travellers.first()
        person.pk = None
        person.save()
        TravelRequest.objects.filter(pk=trip.pk).update(
            created_at=timezone.make_aware(datetime(2025, 1, 15, 12)))
        self.payload['submission_key'] = str(uuid.uuid4())
        self.payload['applicant_email'] = 'other@example.com'
        self.payload['travellers'][0]['email'] = 'other@example.com'
        self.payload['destination'] = 'Kisumu'
        self.post('/api/requests/', self.payload)
        second = TravelRequest.objects.exclude(pk=trip.pk).get()
        TravelRequest.objects.filter(pk=second.pk).update(status='approved',
            created_at=timezone.make_aware(datetime(2025, 12, 31, 23, 59)))
        self.payload['submission_key'] = str(uuid.uuid4())
        self.post('/api/requests/', self.payload)
        TravelRequest.objects.exclude(pk__in=[trip.pk, second.pk]).update(status='rejected',
            created_at=timezone.make_aware(datetime(2026, 1, 1, 0, 1)))
        self.admin()

    def test_annual_analytics_zero_fills_and_respects_year_boundaries(self):
        self.analytics_fixture()
        data = self.client.get('/api/admin/analytics/?year=2025').json()
        self.assertEqual(data['total'], 2)
        self.assertEqual(len(data['monthly']), 12)
        self.assertEqual(data['monthly'][0]['requests'], 1)
        self.assertEqual(data['monthly'][11]['requests'], 1)
        self.assertEqual(data['monthly'][1]['requests'], 0)
        self.assertEqual(data['counts'], {'pending': 1, 'awaiting_admin': 0, 'approved': 1, 'rejected': 0})
        self.assertEqual(self.client.get('/api/admin/analytics/?year=2026').json()['total'], 1)
        self.assertIn(2025, data['available_years'])

    def test_analytics_status_filters_bars_but_preserves_distribution(self):
        self.analytics_fixture()
        data = self.client.get('/api/admin/analytics/?year=2025&status=approved').json()
        self.assertEqual(data['filtered_total'], 1)
        self.assertEqual(sum(m['requests'] for m in data['monthly']), 1)
        self.assertEqual(data['counts']['pending'], 1)
        self.assertEqual(data['top_destinations'], [{'destination': 'Kisumu', 'requests': 1}])
        empty = self.client.get('/api/admin/analytics/?year=2024&status=rejected').json()
        self.assertEqual(empty['total'], 0)
        self.assertTrue(all(m['requests'] == 0 for m in empty['monthly']))

    def test_employee_analytics_scope_and_deduplication(self):
        self.analytics_fixture()
        data = self.client.get('/api/admin/analytics/?year=2025&email=JANE@example.com').json()
        self.assertEqual(data['total'], 1)
        self.assertEqual(data['filtered_total'], 1)
        self.assertEqual(sum(m['requests'] for m in data['monthly']), 1)
        self.assertEqual(data['top_destinations'], [{'destination': 'Mombasa, Kenya', 'requests': 1}])

    def test_analytics_requires_staff_and_valid_filters(self):
        self.assertEqual(self.client.get('/api/admin/analytics/?year=2025').status_code, 403)
        self.admin()
        for query in ['year=abc', 'year=0', 'year=9999', 'status=unknown']:
            self.assertEqual(self.client.get('/api/admin/analytics/?' + query).status_code, 400)

    def test_analytics_years_only_include_scoped_records(self):
        self.analytics_fixture()
        data = self.client.get('/api/admin/analytics/?year=2024').json()
        self.assertEqual(data['available_years'], [2026, 2025])
        self.assertEqual(self.client.get('/api/admin/analytics/').json()['year'], 2026)
        person = self.client.get('/api/admin/analytics/?email=jane@example.com').json()
        self.assertEqual(person['available_years'], [2025])
        self.assertEqual(person['year'], 2025)
        self.assertEqual(person['total'], 1)

    def test_analytics_no_records_has_no_invented_year(self):
        self.admin()
        data = self.client.get('/api/admin/analytics/').json()
        self.assertEqual(data['available_years'], [])
        self.assertIsNone(data['year'])
        self.assertEqual(data['travel_types'], [])
        self.assertEqual(data['total'], 0)

    def test_travel_type_distribution_respects_year_status_and_person(self):
        self.analytics_fixture()
        TravelRequest.objects.filter(status='approved').update(travel_type='local')
        data = self.client.get('/api/admin/analytics/?year=2025').json()
        self.assertEqual(data['travel_types'], [
            {'travel_type': 'air', 'requests': 1}, {'travel_type': 'local', 'requests': 1}])
        selected = self.client.get('/api/admin/analytics/?year=2025&status=approved').json()
        self.assertEqual(selected['travel_types'], [{'travel_type': 'local', 'requests': 1}])
        person = self.client.get('/api/admin/analytics/?email=jane@example.com').json()
        self.assertEqual(person['travel_types'], [{'travel_type': 'air', 'requests': 1}])
        self.assertEqual(sum(t['requests'] for t in data['travel_types']), data['filtered_total'])


    def awaiting_admin(self):
        trip = self.create()
        self.post(f'/api/review/{trip.approval_token}/', {'status': 'approved'})
        trip.refresh_from_db()
        return trip

    @patch('travel.notifications.send_notification_email')
    def test_admin_approval_fanout_private_tickets_and_retry_snapshot(self, send):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from .models import Ticket
        self.payload['travellers'].append({**self.payload['travellers'][0], 'name': 'Other Traveller', 'email': 'other@example.com'})
        trip = self.awaiting_admin()
        self.admin()
        people = list(trip.travellers.all())
        for person in people:
            response = self.client.post(f'/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/',
                {'ticket': SimpleUploadedFile(f'{person.id}.pdf', b'%PDF-1.7 sample ticket', content_type='application/pdf')})
            self.assertEqual(response.status_code, 201)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(f'/api/admin/requests/{trip.pk}/decision/', {'status': 'approved', 'message': 'Have a good trip.',
                'traveller_messages': {str(people[0].id): 'Jane instructions', str(people[1].id): 'Other instructions'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'approved')
        trip.refresh_from_db()
        self.assertIsNotNone(trip.admin_decided_by_id)
        calls = {call.args[0]: call for call in send.call_args_list}
        self.assertEqual(set(calls), {'jane@example.com', 'other@example.com', 'manager@example.com'})
        self.assertEqual(calls['manager@example.com'].kwargs['attachments'], [])
        self.assertEqual(calls['manager@example.com'].kwargs['message'], 'Have a good trip.')
        for person in people:
            call = calls[person.email]
            self.assertEqual(len(call.kwargs['attachments']), 1)
            self.assertEqual(call.kwargs['attachments'][0]['filename'], f'{person.id}.pdf')
            self.assertIsNone(call.args[3])
        self.assertNotIn('Other instructions', calls['jane@example.com'].kwargs['message'])
        self.assertEqual(self.post(f'/api/admin/requests/{trip.pk}/decision/', {'status': 'rejected'}).status_code, 409)
        self.assertEqual(self.client.delete(f'/api/admin/tickets/{Ticket.objects.first().pk}/').status_code, 409)
        # Delayed supervisor notification must still describe its own stage.
        deliver(Notification.objects.get(request=trip, event='decision').pk)
        self.assertEqual(send.call_args.kwargs['status'], 'awaiting_admin')

    def test_admin_create_skips_supervisor_and_requires_staff(self):
        payload = {**self.payload}
        del payload['supervisor_email']
        self.assertEqual(self.post('/api/admin/requests/new/', payload).status_code, 403)
        self.assertEqual(self.post('/api/requests/', payload).status_code, 400)
        self.admin()
        response = self.post('/api/admin/requests/new/', payload)
        self.assertEqual(response.status_code, 201)
        trip = TravelRequest.objects.get()
        self.assertEqual(trip.supervisor_status, 'not_required')
        self.assertEqual(trip.status, 'awaiting_admin')
        self.assertEqual(trip.supervisor_email, '')
        self.assertFalse(trip.notifications.filter(event='supervisor_review').exists())
        self.assertEqual(self.client.get(f'/api/review/{trip.approval_token}/').status_code, 404)
        self.assertEqual(self.post('/api/admin/requests/new/', payload).status_code, 200)

    def test_admin_cannot_bypass_supervisor_or_supply_foreign_traveller(self):
        trip = self.create()
        url = f'/api/admin/requests/{trip.pk}/decision/'
        self.assertEqual(self.post(url, {'status': 'approved'}).status_code, 403)
        self.admin()
        self.assertEqual(self.post(url, {'status': 'approved'}).status_code, 409)
        self.post(f'/api/review/{trip.approval_token}/', {'status': 'approved'})
        self.assertEqual(self.post(url, {'status': 'approved', 'traveller_messages': {'999999': 'foreign'}}).status_code, 400)
        trip.refresh_from_db()
        self.assertEqual(trip.status, 'awaiting_admin')
        self.assertFalse(trip.notifications.filter(event='admin_decision').exists())

    @patch('travel.notifications.send_notification_email')
    def test_admin_rejection_notifies_supervisor_and_applicant_without_tickets(self, send):
        trip = self.awaiting_admin()
        self.admin()
        with self.captureOnCommitCallbacks(execute=True):
            response = self.post(f'/api/admin/requests/{trip.pk}/decision/', {'status': 'rejected', 'reason': 'Budget unavailable'})
        self.assertEqual(response.json()['rejection_reason'], 'Budget unavailable')
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertEqual(call.kwargs['status'], 'rejected')
            self.assertEqual(call.kwargs['reason'], 'Budget unavailable')
            self.assertEqual(call.kwargs['attachments'], [])

    def test_ticket_validation_private_download_and_frozen_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        trip = self.awaiting_admin()
        person = trip.travellers.first()
        url = f'/api/admin/requests/{trip.pk}/travellers/{person.id}/tickets/'
        self.assertEqual(self.client.post(url).status_code, 403)
        self.admin()
        self.assertEqual(self.client.post(url, {'ticket': SimpleUploadedFile('bad.pdf', b'<html>bad</html>')}).status_code, 400)
        self.assertEqual(self.client.post(url, {'ticket': SimpleUploadedFile('large.pdf', b'%PDF-' + b'a'*(5*1024*1024))}).status_code, 400)
        ticket = self.client.post(url, {'ticket': SimpleUploadedFile('ticket.pdf', b'%PDF-1.7 sample')}).json()
        download = f'/api/admin/tickets/{ticket["id"]}/'
        self.assertEqual(self.client.get(download)['Cache-Control'], 'no-store')
        self.assertIn('attachment', self.client.get(download)['Content-Disposition'])
        self.client.logout()
        self.assertEqual(self.client.get(download).status_code, 403)
        self.client.force_login(get_user_model().objects.get(username='admin'))
        self.post(f'/api/admin/requests/{trip.pk}/decision/', {'status': 'approved'})
        self.assertEqual(self.client.post(url, {'ticket': SimpleUploadedFile('ticket.pdf', b'%PDF-1.7 sample')}).status_code, 409)

    def test_global_date_filter_scopes_metrics_list_and_notifications(self):
        trip = self.awaiting_admin()
        TravelRequest.objects.filter(pk=trip.pk).update(created_at=timezone.make_aware(datetime(2024, 2, 14, 12)))
        self.payload['submission_key'] = str(uuid.uuid4())
        self.post('/api/requests/', self.payload)
        self.admin()
        for query in ['year=2024', 'month=2024-02']:
            metrics = self.client.get('/api/admin/metrics/?' + query).json()
            rows = self.client.get('/api/admin/requests/?' + query).json()
            self.assertEqual(metrics['total'], 1)
            self.assertEqual(rows['count'], 1)
            self.assertEqual(metrics['counts']['awaiting_admin'], 1)
            self.assertEqual(metrics['travellers'], 1)
            self.assertEqual(metrics['notification_failures'], 3)
        self.assertEqual(sum(x['requests'] for x in metrics['daily']), 1)
        self.assertEqual(self.client.get('/api/admin/requests/?year=invalid').status_code, 400)
        self.assertEqual(self.client.get('/api/admin/metrics/?month=bad').status_code, 400)


    def test_upgrade_preserves_supervisor_decisions_and_retry_meaning(self):
        from importlib import import_module
        from django.apps import apps
        trip = self.create()
        decided = timezone.now()
        TravelRequest.objects.filter(pk=trip.pk).update(status='approved', decided_at=decided)
        notice = Notification.objects.create(request=trip, event='decision', recipient=trip.applicant_email)
        import_module('travel.migrations.0002_ticket_and_more').migrate_stages(apps, None)
        trip.refresh_from_db()
        notice.refresh_from_db()
        self.assertEqual(trip.status, 'awaiting_admin')
        self.assertEqual(trip.supervisor_status, 'approved')
        self.assertEqual(trip.supervisor_decided_at, decided)
        self.assertIsNone(trip.decided_at)
        self.assertEqual(notice.payload['status'], 'awaiting_admin')
