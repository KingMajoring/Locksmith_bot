from django.db import models


class Locksmith(models.Model):
    """A WGTK locksmith/engineer, kept in sync from Handl.

    Shared across all four tool areas (stock accuracy, job completion,
    job costing, panelled jobs) so each only needs to reference this model
    rather than re-fetching engineer details from Handl each time.
    """

    handl_engineer_id = models.CharField(
        max_length=64,
        unique=True,
        help_text="Engineer/locksmith identifier as used in Handl.",
    )
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
