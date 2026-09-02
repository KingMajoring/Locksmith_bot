from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_completion", "0006_completedjob_loss_type_completedjob_supplied_service_and_more"),
    ]

    operations = [
        migrations.RenameField(
            model_name="slatarget",
            old_name="service_type",
            new_name="loss_type",
        ),
    ]
