from django.db import models


class Locksmith(models.Model):
    """A WGTK locksmith/engineer, kept in sync from Soter (Handl).

    Shared across all four tool areas (stock accuracy, job completion,
    job costing, panelled jobs) so each only needs to reference this model
    rather than re-fetching engineer details from Soter each time.

    Soter's Lookup_Locksmiths table holds every locksmith the business
    deals with — WGTK's own staff (named "WGTK - <name>") and panel/
    subcontractor firms alike (everything else) — which is also how
    Panelled Jobs (Area 4) identifies a job that went to panel: its
    assigned locksmith isn't one of ours. Only current active staff
    (name starts "WGTK -", not "XWGTK -" which marks ex-staff) get
    Locksmith records here.
    """

    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def soter_id_list(self) -> list[str]:
        return list(self.soter_ids.values_list("soter_locksmith_id", flat=True))


class SoterLocksmithId(models.Model):
    """One of a locksmith's Soter Lookup_Locksmiths.ID values.

    Usually two per locksmith — a "(V)" (van) and "(A)" row for the same
    person — whose stock/usage gets combined when querying Soter, since
    Soter tracks them as separate stock locations for one physical
    person.
    """

    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="soter_ids"
    )
    soter_locksmith_id = models.CharField(
        max_length=64, help_text="Lookup_Locksmiths.ID in Soter, e.g. '1163'."
    )

    class Meta:
        unique_together = ("locksmith", "soter_locksmith_id")
        verbose_name = "Soter locksmith ID"

    def __str__(self):
        return f"{self.locksmith} → Soter #{self.soter_locksmith_id}"


class OptimoDriverId(models.Model):
    """A locksmith's Optimo driver identifier (driverSerial), for
    correlating completed-job data pulled from the Optimo API (Area 2)
    back to a WGTK locksmith. Kept separate from soter_id_list since
    Optimo and Soter identify the same person differently.
    """

    locksmith = models.ForeignKey(
        Locksmith, on_delete=models.CASCADE, related_name="optimo_driver_ids"
    )
    optimo_driver_serial = models.CharField(
        max_length=64, help_text="Optimo driverSerial, e.g. '011'."
    )

    class Meta:
        unique_together = ("locksmith", "optimo_driver_serial")
        verbose_name = "Optimo driver ID"

    def __str__(self):
        return f"{self.locksmith} → Optimo #{self.optimo_driver_serial}"
