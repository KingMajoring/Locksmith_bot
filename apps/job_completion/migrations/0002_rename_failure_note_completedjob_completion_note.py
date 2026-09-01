from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("job_completion", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="completedjob",
            old_name="failure_note",
            new_name="completion_note",
        ),
    ]
