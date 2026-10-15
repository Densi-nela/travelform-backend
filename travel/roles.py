"""Application roles use Django's existing User and Group models."""

TRAVEL_ADMIN = "Travel admins"
DEVELOPER = "Developers"


def is_developer(user):
    return (
        user.is_authenticated
        and user.is_active
        and (user.is_superuser or user.groups.filter(name=DEVELOPER).exists())
    )


def is_travel_admin(user):
    return (
        user.is_authenticated
        and user.is_active
        and not is_developer(user)
        and user.groups.filter(name=TRAVEL_ADMIN).exists()
    )


def travel_admin_users():
    from django.contrib.auth import get_user_model

    return (
        get_user_model()
        .objects.filter(is_active=True, is_superuser=False, groups__name=TRAVEL_ADMIN)
        .exclude(groups__name=DEVELOPER)
        .exclude(email="")
        .distinct()
    )
