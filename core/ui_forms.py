from __future__ import annotations

from django import forms

from .models import Appointment, Client, Room, Service, Specialty


class AppointmentForm(forms.ModelForm):
    class Meta:
        model = Appointment
        fields = ["provider", "room", "service", "client", "start_time", "end_time", "status"]
        widgets = {
            "start_time": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "end_time": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ["full_name", "phone_number"]
        widgets = {
            "phone_number": forms.TextInput(attrs={"placeholder": "למשל 0541234567"}),
        }


class RoomForm(forms.ModelForm):
    class Meta:
        model = Room
        fields = ["name", "specialties"]


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ["name", "specialty", "duration_minutes"]
