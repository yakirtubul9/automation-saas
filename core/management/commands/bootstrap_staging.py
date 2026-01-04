import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from core.models import Business


class Command(BaseCommand):
    help = "Bootstrap a staging environment: create an admin user and a demo Business (idempotent)."

    def handle(self, *args, **options):
        username = os.getenv("ADMIN_USERNAME")
        password = os.getenv("ADMIN_PASSWORD")
        email = os.getenv("ADMIN_EMAIL", "admin@example.com")
        business_name = os.getenv("BUSINESS_NAME", "Demo House of Doctors")

        if not username or not password:
            self.stdout.write(
                self.style.WARNING(
                    "bootstrap_staging: ADMIN_USERNAME/ADMIN_PASSWORD not set; skipping admin bootstrap."
                )
            )
            return

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={"email": email})

        if created:
            user.set_password(password)
            user.is_staff = True
            user.is_superuser = True
            if hasattr(user, "email") and not user.email:
                user.email = email
            user.save()
            self.stdout.write(self.style.SUCCESS(f"Created admin user: {username}"))
        else:
            changed = False
            if not user.is_staff:
                user.is_staff = True
                changed = True
            if not user.is_superuser:
                user.is_superuser = True
                changed = True
            if hasattr(user, "email") and email and (not user.email):
                user.email = email
                changed = True
            if changed:
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Updated existing user to admin: {username}"))
            else:
                self.stdout.write(f"Admin user already exists: {username}")

        # Ensure there is at least one Business for this user (needed for dashboard)
        if not Business.objects.filter(owner=user).exists():
            Business.objects.create(owner=user, name=business_name)
            self.stdout.write(self.style.SUCCESS(f"Created demo Business for {username}: {business_name}"))
        else:
            self.stdout.write("Business already exists for admin user; skipping.")
