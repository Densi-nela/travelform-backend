"""Root routes for the Django developer portal and travel API."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [path("django-admin/", admin.site.urls), path("api/", include("travel.urls"))]
