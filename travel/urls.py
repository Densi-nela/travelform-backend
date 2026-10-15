"""API routes for public applications and the travel administration portal."""

from django.urls import path

from . import portal_management
from .views import (
    analytics,
    approvals,
    documents,
    notifications,
    requests,
    sessions,
    submissions,
    tickets,
)

urlpatterns = [
    path("admin/departments/", portal_management.departments),
    path("admin/departments/<int:pk>/", portal_management.departments),
    path("admin/users/", portal_management.users),
    path("admin/users/<int:pk>/", portal_management.users),
    path("admin/requests/<uuid:pk>/send-tickets/", tickets.send_tickets),
    path("departments/", documents.department_options),
    path(
        "admin/requests/<uuid:pk>/documents/<int:document_id>/",
        documents.supporting_document_download,
    ),
    path(
        "review/<uuid:token>/requests/<uuid:pk>/documents/<int:document_id>/",
        documents.supporting_document_download,
    ),
    path("admin/notifications/", notifications.undelivered_notifications),
    path(
        "admin/requests/<uuid:pk>/notifications/<int:notification_id>/retry/",
        notifications.retry_notification,
    ),
    path("admin/requests/new/", submissions.admin_submit),
    path("admin/requests/<uuid:pk>/decision/", approvals.admin_decision),
    path("admin/requests/<uuid:pk>/travellers/<int:traveller_id>/tickets/", tickets.upload_ticket),
    path("admin/tickets/<int:pk>/", tickets.ticket_detail),
    path("health/", sessions.health),
    path("admin/analytics/", analytics.analytics),
    path("session/", sessions.session),
    path("login/", sessions.sign_in),
    path("logout/", sessions.sign_out),
    path("requests/", submissions.submit),
    path("review/<uuid:token>/", approvals.review),
    path("admin/requests/", requests.requests_list),
    path("admin/requests/<uuid:pk>/", requests.request_detail),
    path("admin/metrics/", analytics.metrics),
    path("admin/people/", requests.person_history),
]
