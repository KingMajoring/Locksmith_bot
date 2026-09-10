from django.db import migrations


def seed_vxrp2(apps, schema_editor):
    PartUnitConversion = apps.get_model("stock_accuracy", "PartUnitConversion")
    PartUnitConversion.objects.get_or_create(
        part_code="VXRP2",
        defaults={
            "part_name": "Flip blade pins suitable for Hyundai K",
            "units_per_pack": 10,
            "note": "Reported live: Handl tracks this in packs of 10, but "
            "locksmiths physically count individual pins.",
        },
    )


def unseed_vxrp2(apps, schema_editor):
    PartUnitConversion = apps.get_model("stock_accuracy", "PartUnitConversion")
    PartUnitConversion.objects.filter(part_code="VXRP2").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("stock_accuracy", "0006_partunitconversion"),
    ]

    operations = [
        migrations.RunPython(seed_vxrp2, unseed_vxrp2),
    ]
