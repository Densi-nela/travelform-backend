import uuid
from django.db import models


class TravelRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Separate from the public reference: this UUID is a bearer credential.
    approval_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submission_key = models.UUIDField(unique=True, editable=False)
    travel_type = models.CharField(max_length=20, default='air')
    applicant_name = models.CharField(max_length=150)
    applicant_email = models.EmailField()
    supervisor_email = models.EmailField()
    destination = models.CharField(max_length=200)
    event_description = models.TextField(max_length=5000)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    rejection_reason = models.TextField(blank=True, max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']


class AirTraveller(models.Model):
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


class Notification(models.Model):
    request = models.ForeignKey(TravelRequest, related_name='notifications', on_delete=models.CASCADE)
    event = models.CharField(max_length=40)
    recipient = models.EmailField()
    delivered_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=250, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['request', 'event'], name='one_notification_per_event')]
