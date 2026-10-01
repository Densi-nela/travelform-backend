from django.urls import path
from . import views

urlpatterns = [
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
