from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_appointment_change_proposal"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointmentchangeproposal",
            name="last_error_code",
            field=models.CharField(blank=True, default="", max_length=60),
        ),
        migrations.AddField(
            model_name="appointmentchangeproposal",
            name="last_error_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="appointmentchangeproposal",
            name="last_error_payload",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointmentchangeproposal",
            name="last_attempted_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
