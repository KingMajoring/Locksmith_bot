from django.db import migrations


def seed_3d_job_token(apps, schema_editor):
    VirtualStockItem = apps.get_model("stock_accuracy", "VirtualStockItem")
    VirtualStockItem.objects.get_or_create(
        part_code="3DJT",
        defaults={
            "part_name": "3D Job Token",
            "note": "Reported live: not physical van stock, shouldn't be counted in a stock take.",
        },
    )


def unseed_3d_job_token(apps, schema_editor):
    VirtualStockItem = apps.get_model("stock_accuracy", "VirtualStockItem")
    VirtualStockItem.objects.filter(part_code="3DJT").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("stock_accuracy", "0004_virtualstockitem"),
    ]

    operations = [
        migrations.RunPython(seed_3d_job_token, unseed_3d_job_token),
    ]
