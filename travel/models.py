import uuid
from django.db import models


class Department(models.Model):
    name = models.CharField(max_length=150, unique=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class TravelRequest(models.Model):
    COMPANIES = [(v, v) for v in ['Apa General', 'APA life', 'Apollo Asset', 'Gordon Court', 'APA Micro']]
    company = models.CharField(max_length=50, choices=COMPANIES, blank=True)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.PROTECT)

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending supervisor'
        AWAITING_ADMIN = 'awaiting_admin', 'Awaiting admin action'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Separate from the public reference: this UUID is a bearer credential.
    approval_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submission_key = models.UUIDField(unique=True, editable=False)
    travel_type = models.CharField(max_length=20, default='air')
    applicant_name = models.CharField(max_length=150)
    applicant_email = models.EmailField()
    supervisor_email = models.EmailField(blank=True)
    supervisor_status = models.CharField(max_length=20, default="pending")
    supervisor_decided_at = models.DateTimeField(null=True, blank=True)
    admin_message = models.TextField(blank=True, max_length=5000)
    admin_decided_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="travel_decisions")
    destination = models.CharField(max_length=200)
    event_description = models.TextField(max_length=5000)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    rejection_reason = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']


class AirTraveller(models.Model):
    document_type = models.CharField(max_length=20, choices=[('national_id', 'National ID'), ('passport', 'Passport')], blank=True)
    document_number = models.CharField(max_length=50, blank=True)
    checked_luggage = models.BooleanField(default=False)

    request = models.ForeignKey(TravelRequest, related_name='travellers', on_delete=models.CASCADE)
    name = models.CharField(max_length=150)
    email = models.EmailField(db_index=True)
    departure_date = models.DateField()
    departure_time = models.TimeField()
    return_date = models.DateField()
    return_time = models.TimeField()
    ticket_class = models.CharField(max_length=20, choices=[('economy', 'Economy'), ('premium_economy', 'Premium economy'), ('business', 'Business'), ('first', 'First')])

    class Meta:
        ordering = ['id']


class Ticket(models.Model):
    cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    notification = models.ForeignKey("Notification", null=True, blank=True, on_delete=models.PROTECT, related_name="assigned_tickets")
    traveller = models.ForeignKey(AirTraveller, related_name="tickets", on_delete=models.CASCADE)
    filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    content = models.BinaryField()
    size = models.PositiveIntegerField()


class Notification(models.Model):
    request = models.ForeignKey(TravelRequest, related_name='notifications', on_delete=models.CASCADE)
    event = models.CharField(max_length=40)
    recipient = models.EmailField()
    payload = models.JSONField(default=dict)
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivery_started_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['request', 'event', 'recipient'], name='one_notification_per_recipient', condition=~models.Q(event='tickets'))]


class SupportingDocument(models.Model):
    request = models.ForeignKey(TravelRequest, related_name='supporting_documents', on_delete=models.CASCADE)
    filename = models.CharField(max_length=200)
    content_type = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    object_key = models.CharField(max_length=500, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
