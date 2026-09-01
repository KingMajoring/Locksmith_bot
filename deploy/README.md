# Deploying to Azure

`azure_setup.sh` provisions everything and deploys the current checkout in
one go. Run it **on your own machine**, not in this sandboxed session — it
needs the real Azure CLI and your Azure login.

## Prerequisites

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) installed
- `az login` completed (or `az login --use-device-code` if you're on a
  headless machine)
- An Azure subscription with permission to create resource groups, App
  Service, Postgres Flexible Server, and Azure AD app registrations
- This repo cloned and checked out to the commit you want deployed

## Run it

```bash
cd Locksmith_bot
git checkout claude/multi-area-management-tool-1cn4og   # or whichever branch
```

Open `deploy/azure_setup.sh` and edit the variables at the top:

- `RESOURCE_GROUP`, `LOCATION` — where things get created
- `APP_NAME`, `DB_SERVER_NAME` — must be globally unique across all of Azure;
  the script will fail on these steps if someone else already has them
- `WGTK_EMAIL_DOMAIN` — the real Microsoft 365 domain locksmith-office staff
  sign in with, so login is restricted to your organisation

Then:

```bash
bash deploy/azure_setup.sh
```

It creates:
- a resource group
- a Postgres Flexible Server + database
- a Linux App Service (Python 3.12) running this app via `startup.sh`
  (`migrate` + `collectstatic` + gunicorn)
- an Azure AD app registration for the "Sign in with Microsoft" button,
  scoped to your tenant only (`AzureADMyOrg`)
- deploys the current git checkout via zip deploy

It prints the app URL and the generated Postgres admin password (save that
password — it isn't shown again) at the end, along with the remaining
`az webapp config appsettings set` commands for Handl DB access and M365
SMTP sending once those details are confirmed (see the main `README.md`).

## First login / access

Once `MICROSOFT_CLIENT_ID`/`MICROSOFT_CLIENT_SECRET`/`MICROSOFT_TENANT_ID`
are set (the script does this), anyone with a `@<WGTK_EMAIL_DOMAIN>`
Microsoft 365 account can sign in and reach the app. For a superuser
(needed for the Django `/admin/` config screens — locksmiths, schedules,
thresholds), either:

- promote a Microsoft-SSO user to staff/superuser afterwards via
  `az webapp ssh` → `python manage.py shell`, or
- create a local superuser the same way: `az webapp ssh` →
  `python manage.py createsuperuser`.

## Redeploying after code changes

```bash
git archive --format=zip -o /tmp/wgtk-ops-tool-deploy.zip HEAD
az webapp deploy --resource-group "$RESOURCE_GROUP" --name "$APP_NAME" \
  --src-path /tmp/wgtk-ops-tool-deploy.zip --type zip
```

## Tearing it down

```bash
az group delete --name "$RESOURCE_GROUP" --yes --no-wait
```

This deletes everything created above (App Service, Postgres, the
resource group itself). It does **not** delete the Azure AD app
registration — remove that separately if you want:

```bash
az ad app delete --id "$AD_APP_ID"
```
