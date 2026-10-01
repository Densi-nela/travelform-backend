from datetime import date, time, datetime
from uuid import UUID
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone


def text(data, key, limit, optional=False):
    value = data.get(key, '')
    if not isinstance(value, str) or (not optional and not value.strip()) or len(value.strip()) > limit:
        raise ValueError(f'{key.replace("_", " ").capitalize()} is required and must be at most {limit} characters.')
    return value.strip()


def email(data, key):
    value = text(data, key, 254).lower()
    try:
        validate_email(value)
    except ValidationError:
        raise ValueError(f'Enter a valid {key.replace("_", " ")}.')
    return value


def submission(data):
    if not isinstance(data, dict):
        raise ValueError('Expected a JSON object.')
    try:
        key = UUID(str(data.get('submission_key', '')))
    except ValueError:
        raise ValueError('A valid submission key is required.')
    if data.get('travel_type', 'air') != 'air':
        raise ValueError('Only air travel is currently supported.')
    trip = {'submission_key': key, 'applicant_name': text(data, 'applicant_name', 150),
            'applicant_email': email(data, 'applicant_email'), 'supervisor_email': email(data, 'supervisor_email'),
            'destination': text(data, 'destination', 200), 'event_description': text(data, 'event_description', 5000)}
    if trip['applicant_email'] == trip['supervisor_email']:
        raise ValueError('Supervisor email must differ from applicant email.')
    travellers = data.get('travellers')
    if not isinstance(travellers, list) or not 1 <= len(travellers) <= 50:
        raise ValueError('Add between 1 and 50 travellers.')
    cleaned = []
    for i, person in enumerate(travellers, 1):
        try:
            if not isinstance(person, dict):
                raise ValueError('Invalid traveller.')
            row = {'name': text(person, 'name', 150), 'email': email(person, 'email')}
            for key in ['departure_date', 'return_date']:
                row[key] = date.fromisoformat(str(person.get(key, '')))
            for key in ['departure_time', 'return_time']:
                row[key] = time.fromisoformat(str(person.get(key, '')))
                if row[key].tzinfo:
                    raise ValueError('Use local destination times without timezone offsets.')
            if row['departure_date'] < timezone.localdate():
                raise ValueError('Departure cannot be in the past.')
            if datetime.combine(row['return_date'], row['return_time']) <= datetime.combine(row['departure_date'], row['departure_time']):
                raise ValueError('Return must be after departure.')
            row['ticket_class'] = person.get('ticket_class', '')
            if row['ticket_class'] not in ['economy', 'premium_economy', 'business', 'first']:
                raise ValueError('Choose a valid ticket class.')
            cleaned.append(row)
        except (ValueError, TypeError) as exc:
            raise ValueError(f'Traveller {i}: {exc}')
    return trip, cleaned
