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


class TeamsShiftsSettings(models.Model):
    """WGTK's own rota Team ID for Microsoft Teams Shifts, editable via
    the admin rather than an Azure app setting — same single-row
    pattern as OptimoSettings/GoogleMapsSettings above. Not a secret
    itself (Graph auth reuses the existing MS_GRAPH_MAIL_* app
    registration — see apps/integrations/teams_shifts.py), just stored
    the same way so it can be set/changed without a redeploy.

    Falls back to the MS_GRAPH_TEAM_ID app setting if no row exists yet
    or its team_id is blank.
    """

    team_id = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Teams Shifts settings"
        verbose_name_plural = "Teams Shifts settings"

    def __str__(self):
        return "Teams Shifts settings"

    @classmethod
    def current_team_id(cls) -> str:
        obj = cls.objects.first()
        return obj.team_id if obj else ""


class GoogleMapsSettings(models.Model):
    """The Google Maps (Distance Matrix) API key, editable via the admin
    rather than an Azure app setting — same rationale and single-row
    pattern as OptimoSettings above.

    Falls back to the GOOGLE_MAPS_API_KEY app setting (see
    apps/integrations/google_maps.py get_google_maps_client()) if no row
    exists yet or its api_key is blank.
    """

    api_key = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Google Maps API settings"
        verbose_name_plural = "Google Maps API settings"

    def __str__(self):
        return "Google Maps API settings"

    @classmethod
    def current_key(cls) -> str:
        obj = cls.objects.first()
        return obj.api_key if obj else ""
