from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_whatsappmessage_conversationsession"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="whatsapp_phone_number_id",
            field=models.CharField(
                blank=True,
                null=True,
                db_index=True,
                help_text="WhatsApp Cloud API phone_number_id for reliable routing",
                max_length=64,
            ),
        ),
    ]
