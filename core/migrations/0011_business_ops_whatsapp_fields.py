from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0010_provider_whatsapp_phone_number_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="business",
            name="ops_whatsapp_display_number",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.AddField(
            model_name="business",
            name="ops_whatsapp_phone_number_id",
            field=models.CharField(blank=True, db_index=True, default="", max_length=64),
        ),
    ]
