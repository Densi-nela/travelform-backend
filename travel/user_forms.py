from django import forms
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django.contrib.auth.models import Group
from .roles import TRAVEL_ADMIN, DEVELOPER


class RoleFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields['role'].initial = DEVELOPER if self.instance.is_superuser or self.instance.groups.filter(name=DEVELOPER).exists() else TRAVEL_ADMIN

    def clean_role(self):
        role = self.cleaned_data['role']
        if self.instance.is_superuser and role != DEVELOPER:
            raise forms.ValidationError('Superusers are developers. Create a separate travel-admin account for the travel portal.')
        return role

    def clean_email(self):
        return self.cleaned_data['email'].strip().lower()

    def save(self, commit=True):
        self.instance.is_staff = self.cleaned_data['role'] == DEVELOPER
        return super().save(commit=commit)

    def _save_m2m(self):
        super()._save_m2m()
        groups = [Group.objects.get_or_create(name=name)[0] for name in [TRAVEL_ADMIN, DEVELOPER]]
        self.instance.groups.remove(*groups)
        self.instance.groups.add(next(g for g in groups if g.name == self.cleaned_data['role']))


class PortalUserCreationForm(RoleFormMixin, UserCreationForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=[(TRAVEL_ADMIN, 'Travel admin'), (DEVELOPER, 'Developer')])

    class Meta(UserCreationForm.Meta):
        fields = ('username', 'email', 'first_name', 'last_name', 'role')


class PortalUserChangeForm(RoleFormMixin, UserChangeForm):
    email = forms.EmailField(required=True)
    role = forms.ChoiceField(choices=[(TRAVEL_ADMIN, 'Travel admin'), (DEVELOPER, 'Developer')])
