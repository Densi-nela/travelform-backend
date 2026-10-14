from django.db import migrations


def assign_roles(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Group = apps.get_model('auth', 'Group')
    travel, _ = Group.objects.get_or_create(name='Travel admins')
    developer, _ = Group.objects.get_or_create(name='Developers')
    for user in User.objects.filter(is_staff=True):
        user.groups.add(developer if user.is_superuser else travel)
        if not user.is_superuser:
            user.is_staff = False
            user.save(update_fields=['is_staff'])


class Migration(migrations.Migration):
    dependencies = [('travel', '0007_ticket_cost'), ('auth', '0012_alter_user_first_name_max_length')]
    operations = [migrations.RunPython(assign_roles, migrations.RunPython.noop)]
