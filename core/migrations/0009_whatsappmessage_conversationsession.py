from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0008_weeklyreportlog_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="WhatsAppMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("direction", models.CharField(choices=[("in", "Inbound"), ("out", "Outbound")], max_length=8)),
                ("purpose", models.CharField(choices=[("reminder", "Reminder"), ("client_agent", "Client agent"), ("ops_agent", "Ops agent"), ("other", "Other")], default="other", max_length=20)),
                ("wa_message_id", models.CharField(blank=True, default="", max_length=200)),
                ("from_number", models.CharField(blank=True, default="", max_length=50)),
                ("to_number", models.CharField(blank=True, default="", max_length=50)),
                ("body", models.TextField(blank=True, default="")),
                ("raw_payload", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="whatsapp_messages", to="core.business")),
                ("client", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="whatsapp_messages", to="core.client")),
                ("provider", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="whatsapp_messages", to="core.provider")),
            ],
            options={
                "indexes": [
                    models.Index(fields=["business", "created_at"], name="core_whatsa_business_0f1a8b_idx"),
                    models.Index(fields=["business", "direction", "created_at"], name="core_whatsa_business_f8d419_idx"),
                    models.Index(fields=["wa_message_id"], name="core_whatsa_wa_mess_2eb4c9_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="ConversationSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("wa_from_number", models.CharField(max_length=50)),
                ("state", models.JSONField(blank=True, default=dict)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("business", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conversation_sessions", to="core.business")),
                ("provider", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="conversation_sessions", to="core.provider")),
            ],
            options={
                "unique_together": {("business", "provider", "wa_from_number")},
                "indexes": [
                    models.Index(fields=["business", "provider", "wa_from_number"], name="core_conver_business_0c6535_idx"),
                    models.Index(fields=["expires_at"], name="core_conver_expires_902f37_idx"),
                ],
            },
        ),
    ]
