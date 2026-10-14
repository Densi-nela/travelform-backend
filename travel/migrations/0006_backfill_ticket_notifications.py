from django.db import migrations


def link_existing(apps, schema_editor):
    Notification = apps.get_model('travel', 'Notification')
    Ticket = apps.get_model('travel', 'Ticket')
    for notice in Notification.objects.filter(event='admin_decision').iterator():
        if notice.payload.get('status') == 'approved':
            Ticket.objects.filter(pk__in=notice.payload.get('ticket_ids', []),
                traveller__request_id=notice.request_id, traveller__email__iexact=notice.recipient,
                notification__isnull=True).update(notification_id=notice.pk)


class Migration(migrations.Migration):
    dependencies = [('travel', '0005_remove_notification_one_notification_per_recipient_and_more')]
    operations = [migrations.RunPython(link_existing, migrations.RunPython.noop)]
