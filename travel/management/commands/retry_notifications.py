from django.core.management.base import BaseCommand

from travel.models import Notification
from travel.notifications import deliver


class Command(BaseCommand):
    help = "Retry undelivered notifications. Run with one scheduler/worker; API delivery is not guaranteed to be idempotent."

    def handle(self, *args, **options):
        ids = list(
            Notification.objects.filter(delivered_at__isnull=True).values_list("id", flat=True)
        )
        for notification_id in ids:
            deliver(notification_id)
        self.stdout.write(f"Attempted {len(ids)} pending notifications.")
