"""Developer administration for user roles, departments and read-only travel records."""

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import Group

from .models import AirTraveller, Department, TravelRequest
from .roles import DEVELOPER, is_developer
from .user_forms import PortalUserChangeForm, PortalUserCreationForm

admin.site.site_header = "APA developer administration"
admin.site.site_title = "APA administration"
admin.site.has_permission = lambda request: is_developer(request.user)


class DeveloperAccessMixin:
    def has_module_permission(self, request):
        return is_developer(request.user)

    def has_view_permission(self, request, obj=None):
        return is_developer(request.user)

    def has_add_permission(self, request):
        return is_developer(request.user)

    def has_change_permission(self, request, obj=None):
        return is_developer(request.user)

    def has_delete_permission(self, request, obj=None):
        return False  # Deactivate accounts/departments to preserve historical references.


class TravellerInline(admin.TabularInline):
    model = AirTraveller
    extra = 0
    can_delete = False
    readonly_fields = [
        "name",
        "email",
        "document_type",
        "document_number",
        "checked_luggage",
        "departure_date",
        "departure_time",
        "return_date",
        "return_time",
        "ticket_class",
    ]

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TravelRequest)
class TravelRequestAdmin(DeveloperAccessMixin, admin.ModelAdmin):
    list_display = ["applicant_name", "destination", "supervisor_email", "status", "created_at"]
    list_filter = ["status", "travel_type", "company", "department"]
    search_fields = ["applicant_name", "applicant_email", "destination"]
    exclude = ["approval_token", "submission_key"]
    readonly_fields = [
        "company",
        "department",
        "id",
        "travel_type",
        "applicant_name",
        "applicant_email",
        "supervisor_email",
        "destination",
        "event_description",
        "status",
        "rejection_reason",
        "created_at",
        "decided_at",
        "supervisor_status",
        "supervisor_decided_at",
        "admin_message",
        "admin_decided_by",
    ]
    inlines = [TravellerInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Department)
class DepartmentAdmin(DeveloperAccessMixin, admin.ModelAdmin):
    list_display = ["name", "active"]
    search_fields = ["name"]
    list_filter = ["active"]


admin.site.unregister(get_user_model())
admin.site.unregister(Group)


@admin.register(get_user_model())
class PortalUserAdmin(DeveloperAccessMixin, UserAdmin):
    form = PortalUserChangeForm
    add_form = PortalUserCreationForm
    list_display = ["username", "email", "first_name", "last_name", "user_role", "is_active"]
    list_filter = ["is_active", "groups"]
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("User details", {"fields": ("first_name", "last_name", "email", "role", "is_active")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "fields": (
                    "username",
                    "email",
                    "first_name",
                    "last_name",
                    "role",
                    "password1",
                    "password2",
                )
            },
        ),
    )
    filter_horizontal = ()

    @admin.display(description="Role")
    def user_role(self, obj):
        if obj.is_superuser or obj.groups.filter(name=DEVELOPER).exists():
            return "Developer"
        return "Travel admin" if obj.groups.filter(name="Travel admins").exists() else "Unassigned"
