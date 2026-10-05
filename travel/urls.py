from django.urls import path
from . import views

urlpatterns = [
    path('admin/requests/new/', views.admin_submit),
    path('admin/requests/<uuid:pk>/decision/', views.admin_decision),
    path('admin/requests/<uuid:pk>/travellers/<int:traveller_id>/tickets/', views.upload_ticket),
    path('admin/tickets/<int:pk>/', views.ticket_detail),
    path('health/', views.health),
    path('admin/analytics/', views.analytics),
    path('session/', views.session), 
    path('login/', views.sign_in), 
    path('logout/', views.sign_out),
    path('requests/', views.submit), 
    path('review/<uuid:token>/', views.review),
    path('admin/requests/', views.requests_list), 
    path('admin/requests/<uuid:pk>/', views.request_detail),
    path('admin/metrics/', views.metrics), 
    path('admin/people/', views.person_history),
]
