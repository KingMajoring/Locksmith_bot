from django.db import models


class OptimoSettings(models.Model):
    """The Optimo API key, editable via the admin rather than an Azure
    app setting — so it can be rotated by office staff without needing
    a redeploy or Azure CLI access. A single row is used tool-wide,
    same pattern as VarianceThreshold/SLATarget elsewhere.

    Falls back to the OPTIMO_API_KEY app setting (see
    apps/integrations/optimo.py get_optimo_client()) if no row exists
    yet or its api_key is blank.
    """

    api_key = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Optimo API settings"
        verbose_name_plural = "Optimo API settings"

    def __str__(self):
        return "Optimo API settings"

    @classmethod
    def current_key(cls) -> str:
        obj = cls.objects.first()
        return obj.api_key if obj else ""
