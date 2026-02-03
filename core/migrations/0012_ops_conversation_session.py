from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0011_business_ops_whatsapp_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="OpsConversationSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("wa_from_number", models.CharField(max_length=50)),
                ("state", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "business",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="ops_conversation_sessions", to="core.business"),
                ),
                (
                    "membership",
                    models.ForeignKey(
                        help_text="Owner/Staff membership that initiated the conversation",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="ops_conversation_sessions",
                        to="core.businessmembership",
                    ),
                ),
            ],
            options={
                "unique_together": {("business", "wa_from_number")},
            },
        ),
    ]
