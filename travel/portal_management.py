from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from .models import Department
from .roles import TRAVEL_ADMIN, DEVELOPER
from .validation import text, email
from .views import staff_only, body, error


def page_result(request, queryset, serialize):
    page = Paginator(queryset, 20).get_page(request.GET.get('page', 1))
    return JsonResponse({'results': [serialize(obj) for obj in page], 'count': page.paginator.count,
                         'page': page.number, 'pages': page.paginator.num_pages})


def department_data(obj):
    return {'id': obj.pk, 'name': obj.name, 'active': obj.active}


def user_data(obj):
    return {'id': obj.pk, 'username': obj.username, 'first_name': obj.first_name,
            'last_name': obj.last_name, 'email': obj.email, 'active': obj.is_active}


def active_value(data, default):
    value = data.get('active', default)
    if not isinstance(value, bool):
        raise ValueError('Active must be true or false.')
    return value


@require_http_methods(['GET', 'POST', 'PATCH'])
@staff_only
def departments(request, pk=None):
    if request.method == 'GET':
        return page_result(request, Department.objects.filter(name__icontains=request.GET.get('search', '')[:150]), department_data)
    if (request.method == 'PATCH') != (pk is not None):
        return error('Invalid department operation.', 405)
    try:
        data = body(request)
        name = text(data, 'name', 150)
        active = active_value(data, True)
        with transaction.atomic():
            if Department.objects.filter(name__iexact=name).exclude(pk=pk).exists():
                return error('A department with that name already exists.')
            if pk is None:
                obj = Department.objects.create(name=name, active=active)
            else:
                obj = Department.objects.select_for_update().filter(pk=pk).first()
                if obj is None:
                    return error('Department not found.', 404)
                obj.name, obj.active = name, active
                obj.save(update_fields=['name', 'active'])
        return JsonResponse(department_data(obj), status=201 if pk is None else 200)
    except (ValueError, IntegrityError) as exc:
        return error(str(exc) if isinstance(exc, ValueError) else 'That department already exists.')


@require_http_methods(['GET', 'POST', 'PATCH'])
@staff_only
def users(request, pk=None):
    User = get_user_model()
    qs = User.objects.filter(groups__name=TRAVEL_ADMIN, is_superuser=False).exclude(groups__name=DEVELOPER).distinct()
    if request.method == 'GET':
        search = request.GET.get('search', '')[:150]
        return page_result(request, qs.filter(Q(username__icontains=search) | Q(email__icontains=search) | Q(first_name__icontains=search) | Q(last_name__icontains=search)).order_by('username'), user_data)
    if (request.method == 'PATCH') != (pk is not None):
        return error('Invalid user operation.', 405)
    try:
        data = body(request)
        if any(field in data for field in ['role', 'groups', 'is_superuser', 'is_staff', 'user_permissions']):
            return error('This portal can manage travel-admin accounts only.', 403)
        first_name, last_name = text(data, 'first_name', 150, optional=True), text(data, 'last_name', 150, optional=True)
        address = email(data, 'email')
        active = active_value(data, True)
        if pk == request.user.pk and not active:
            return error('You cannot deactivate your own account.')
        with transaction.atomic():
            if pk is None:
                username = text(data, 'username', 150)
                password = data.get('password', '')
                if not isinstance(password, str) or not 1 <= len(password) <= 128:
                    raise ValueError('A password of at most 128 characters is required.')
                obj = User(username=username, email=address, first_name=first_name, last_name=last_name, is_active=active, is_staff=False)
                obj.full_clean(exclude=['password'])
                validate_password(password, user=obj)
                obj.set_password(password)
                obj.save()
                obj.groups.add(Group.objects.get_or_create(name=TRAVEL_ADMIN)[0])
            else:
                obj = qs.select_for_update().filter(pk=pk).first()
                if obj is None:
                    return error('Travel-admin account not found.', 404)
                obj.first_name, obj.last_name, obj.email, obj.is_active = first_name, last_name, address, active
                obj.save(update_fields=['first_name', 'last_name', 'email', 'is_active'])
        return JsonResponse(user_data(obj), status=201 if pk is None else 200)
    except ValidationError as exc:
        return error(' '.join(exc.messages))
    except (ValueError, IntegrityError) as exc:
        return error(str(exc) if isinstance(exc, ValueError) else 'That username already exists.')
