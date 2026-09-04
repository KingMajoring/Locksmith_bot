from django.db import migrations

# Mirrors the hardcoded defaults this replaces (views._AFTER_PHOTO_SLOTS_BY_SERVICE
# and views._access_method_photo_slots) so existing behaviour is unchanged
# until office edits these rows in the admin.
SEED_PROMPTS = [
    # (service_label, step, kind, required, order)
    ("Default", "complete", "after", True, 0),
    ("Gain access", "access_method", "door_frame", True, 0),
    ("AKL", "complete", "front_of_car", True, 0),
    ("AKL", "complete", "door_lock", True, 1),
    ("AKL", "complete", "damage", False, 2),
    ("AKL", "complete", "keys_supplied", True, 3),
    ("AKL", "complete", "ignition_on", True, 4),
    ("Spare Key", "complete", "front_of_car", True, 0),
    ("Spare Key", "complete", "door_lock", True, 1),
    ("Spare Key", "complete", "damage", False, 2),
    ("Spare Key", "complete", "keys_supplied", True, 3),
    ("Spare Key", "complete", "client_key", True, 4),
    ("Spare Key", "complete", "ignition_on", True, 5),
]


def seed_prompts(apps, schema_editor):
    PortalPhotoPrompt = apps.get_model("locksmith_portal", "PortalPhotoPrompt")
    PortalSettings = apps.get_model("locksmith_portal", "PortalSettings")
    for service_label, step, kind, required, order in SEED_PROMPTS:
        PortalPhotoPrompt.objects.get_or_create(
            service_label=service_label, step=step, kind=kind,
            defaults={"required": required, "order": order},
        )
    PortalSettings.objects.get_or_create(pk=1)


def unseed_prompts(apps, schema_editor):
    PortalPhotoPrompt = apps.get_model("locksmith_portal", "PortalPhotoPrompt")
    for service_label, step, kind, _required, _order in SEED_PROMPTS:
        PortalPhotoPrompt.objects.filter(service_label=service_label, step=step, kind=kind).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("locksmith_portal", "0004_portalsettings_portalphotoprompt"),
    ]

    operations = [
        migrations.RunPython(seed_prompts, unseed_prompts),
    ]
