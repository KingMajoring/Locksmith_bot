#!/bin/bash
# One-time Azure provisioning + deploy for the WGTK Ops Tool.
#
# Run this on your own machine, where you already have the Azure CLI
# installed and are logged in (`az login`). It creates: a resource group,
# a Postgres Flexible Server + database, a Linux App Service running this
# app, and an Azure AD app registration for the Microsoft 365 login.
#
# Edit the variables below first. Re-running is mostly safe (az create
# commands are idempotent for unchanged resources) but review before
# re-running against a live environment.
set -euo pipefail

# --- Edit these -------------------------------------------------------
RESOURCE_GROUP="wgtk-ops-tool-rg"
LOCATION="uksouth"
APP_NAME="wgtk-ops-tool"                # must be globally unique across Azure
DB_SERVER_NAME="wgtk-ops-tool-db"       # must be globally unique across Azure
DB_ADMIN_USER="wgtkadmin"
DB_NAME="wgtk_ops_tool"
WGTK_EMAIL_DOMAIN="wgtk.co.uk"          # real WGTK Microsoft 365 domain
# ------------------------------------------------------------------------

APP_URL="https://${APP_NAME}.azurewebsites.net"
DB_ADMIN_PASSWORD=$(openssl rand -base64 24)
DJANGO_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(50))")

echo "==> Resource group"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION"

echo "==> Postgres Flexible Server (this takes a few minutes)"
az postgres flexible-server create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$DB_SERVER_NAME" \
  --location "$LOCATION" \
  --admin-user "$DB_ADMIN_USER" \
  --admin-password "$DB_ADMIN_PASSWORD" \
  --sku-name Standard_B1ms \
  --tier Burstable \
  --storage-size 32 \
  --version 16 \
  --public-access 0.0.0.0 \
  --yes

az postgres flexible-server db create \
  --resource-group "$RESOURCE_GROUP" \
  --server-name "$DB_SERVER_NAME" \
  --database-name "$DB_NAME"

DATABASE_URL="postgres://${DB_ADMIN_USER}:${DB_ADMIN_PASSWORD}@${DB_SERVER_NAME}.postgres.database.azure.com:5432/${DB_NAME}?sslmode=require"

echo "==> App Service plan + web app (Python 3.12, Linux)"
az appservice plan create \
  --resource-group "$RESOURCE_GROUP" \
  --name "${APP_NAME}-plan" \
  --location "$LOCATION" \
  --is-linux \
  --sku B1

az webapp create \
  --resource-group "$RESOURCE_GROUP" \
  --plan "${APP_NAME}-plan" \
  --name "$APP_NAME" \
  --runtime "PYTHON:3.12"

az webapp config set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --startup-file "bash startup.sh"

echo "==> Azure AD app registration (Microsoft 365 login)"
AD_APP_ID=$(az ad app create \
  --display-name "WGTK Ops Tool" \
  --sign-in-audience AzureADMyOrg \
  --web-redirect-uris "${APP_URL}/accounts/microsoft/login/callback/" \
  --query appId -o tsv)
AD_APP_SECRET=$(az ad app credential reset --id "$AD_APP_ID" --query password -o tsv)
AD_TENANT_ID=$(az account show --query tenantId -o tsv)
# Microsoft Graph User.Read (delegated) — lets the app read the signed-in
# user's basic profile/email, which is all this login needs.
az ad app permission add --id "$AD_APP_ID" \
  --api 00000003-0000-0000-c000-000000000000 \
  --api-permissions e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope
az ad app permission grant --id "$AD_APP_ID" --api 00000003-0000-0000-c000-000000000000

echo "==> App settings"
az webapp config appsettings set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --settings \
    DJANGO_SETTINGS_MODULE="config.settings.prod" \
    DJANGO_SECRET_KEY="$DJANGO_SECRET_KEY" \
    DATABASE_URL="$DATABASE_URL" \
    ALLOWED_EMAIL_DOMAINS="$WGTK_EMAIL_DOMAIN" \
    MICROSOFT_CLIENT_ID="$AD_APP_ID" \
    MICROSOFT_CLIENT_SECRET="$AD_APP_SECRET" \
    MICROSOFT_TENANT_ID="$AD_TENANT_ID" \
    SCM_DO_BUILD_DURING_DEPLOYMENT=true

echo "==> Deploying code (zip deploy from this checkout)"
git archive --format=zip -o /tmp/wgtk-ops-tool-deploy.zip HEAD
az webapp deploy \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --src-path /tmp/wgtk-ops-tool-deploy.zip \
  --type zip

cat <<EOF

Done. App: ${APP_URL}
Postgres admin password (save this securely, not printed again): ${DB_ADMIN_PASSWORD}

Still to do:
- Handl DB access: set HANDL_DATABASE_URL once confirmed
    az webapp config appsettings set -g $RESOURCE_GROUP -n $APP_NAME --settings HANDL_DATABASE_URL="..."
- M365 SMTP sending: set EMAIL_BACKEND to the SMTP backend + EMAIL_HOST_USER/PASSWORD
    az webapp config appsettings set -g $RESOURCE_GROUP -n $APP_NAME --settings \\
      EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend" \\
      EMAIL_HOST_USER="..." EMAIL_HOST_PASSWORD="..."
- Locksmith records + stock check schedules: add via ${APP_URL}/admin/ once you've
  signed in with a WGTK Microsoft 365 account (or create a local superuser via
  'az webapp ssh' -> python manage.py createsuperuser for first access).
EOF
