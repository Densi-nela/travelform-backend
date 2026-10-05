from django.contrib import admin
from .models import TravelRequest, AirTraveller, Notification


class TravellerInline(admin.TabularInline):
    model = AirTraveller
    extra = 0
    can_delete = False
    readonly_fields = ['name', 'email', 'departure_date', 'departure_time', 'return_date', 'return_time', 'ticket_class']

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TravelRequest)
class TravelRequestAdmin(admin.ModelAdmin):
    list_display = ['applicant_name', 'destination', 'supervisor_email', 'status', 'created_at']
    list_filter = ['status', 'travel_type']
    search_fields = ['applicant_name', 'applicant_email', 'destination']
    exclude = ['approval_token', 'submission_key']
    readonly_fields = ['id', 'travel_type', 'applicant_name', 'applicant_email', 'supervisor_email',
                       'destination', 'event_description', 'status', 'rejection_reason', 'created_at', 'decided_at', 'supervisor_status', 'supervisor_decided_at', 'admin_message', 'admin_decided_by']
    inlines = [TravellerInline]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
