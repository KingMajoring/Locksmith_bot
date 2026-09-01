#!/bin/bash
# Move secrets for the already-deployed WGTK Ops Tool into Azure Key
# Vault, and wire the app up to read them from there instead of plain
# app settings.
#
# Run this AFTER azure_setup.sh has already deployed the app once (it
# reads the existing app settings to migrate their current values).
# Safe to re-run: every step checks before creating/assigning.
set -euo pipefail

# --- Edit these to match azure_setup.sh -------------------------------
RESOURCE_GROUP="wgtk-ops-tool-rg"
LOCATION="uksouth"
APP_NAME="wgtk-ops-tool"
KEY_VAULT_NAME="wgtk-ops-tool-kv"   # must be globally unique, 3-24 chars
# ------------------------------------------------------------------------

echo "==> Key Vault"
if ! az keyvault show --name "$KEY_VAULT_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  az keyvault create \
    --name "$KEY_VAULT_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --location "$LOCATION" \
    --enable-rbac-authorization true
fi
VAULT_ID=$(az keyvault show --name "$KEY_VAULT_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)

echo "==> Letting you manage secrets in it"
CURRENT_USER_ID=$(az ad signed-in-user show --query id -o tsv)
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee "$CURRENT_USER_ID" \
  --scope "$VAULT_ID" \
  --only-show-errors || echo "    (already assigned, or you already have broader access)"

echo "==> App Service managed identity"
PRINCIPAL_ID=$(az webapp identity assign \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --query principalId -o tsv)

echo "==> Letting the app read secrets from it"
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee "$PRINCIPAL_ID" \
  --scope "$VAULT_ID" \
  --only-show-errors || echo "    (already assigned)"

echo "==> Migrating existing app settings into Key Vault"
# Grab current plaintext values so nothing needs retyping.
DJANGO_SECRET_KEY=$(az webapp config appsettings list -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query "[?name=='DJANGO_SECRET_KEY'].value" -o tsv)
DATABASE_URL=$(az webapp config appsettings list -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query "[?name=='DATABASE_URL'].value" -o tsv)
MICROSOFT_CLIENT_SECRET=$(az webapp config appsettings list -g "$RESOURCE_GROUP" -n "$APP_NAME" \
  --query "[?name=='MICROSOFT_CLIENT_SECRET'].value" -o tsv)

az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name "DJANGO-SECRET-KEY" --value "$DJANGO_SECRET_KEY" -o none
az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name "DATABASE-URL" --value "$DATABASE_URL" -o none
az keyvault secret set --vault-name "$KEY_VAULT_NAME" --name "MICROSOFT-CLIENT-SECRET" --value "$MICROSOFT_CLIENT_SECRET" -o none

kv_ref() {
  echo "@Microsoft.KeyVault(VaultName=${KEY_VAULT_NAME};SecretName=${1})"
}

echo "==> Pointing app settings at Key Vault references"
az webapp config appsettings set \
  --resource-group "$RESOURCE_GROUP" \
  --name "$APP_NAME" \
  --settings \
    DJANGO_SECRET_KEY="$(kv_ref DJANGO-SECRET-KEY)" \
    DATABASE_URL="$(kv_ref DATABASE-URL)" \
    MICROSOFT_CLIENT_SECRET="$(kv_ref MICROSOFT-CLIENT-SECRET)" \
  -o none

cat <<EOF

Done. Existing secrets now live in Key Vault '$KEY_VAULT_NAME', referenced
from app settings rather than stored there in plain text.

Next: add the Soter (Handl) SQL credentials the same way, once you have
the ExcelReader password. Nobody needs to paste it into chat — set it
directly:

  az keyvault secret set --vault-name $KEY_VAULT_NAME --name HANDL-SQL-PASSWORD --value '<the real password>'

  az webapp config appsettings set -g $RESOURCE_GROUP -n $APP_NAME --settings \\
    HANDL_SQL_SERVER="soterlive1.database.windows.net" \\
    HANDL_SQL_DATABASE="soter_live" \\
    HANDL_SQL_USER="ExcelReader" \\
    HANDL_SQL_PASSWORD="@Microsoft.KeyVault(VaultName=$KEY_VAULT_NAME;SecretName=HANDL-SQL-PASSWORD)"

(HANDL_SQL_SERVER/DATABASE/USER aren't secret on their own, but keeping
the password in Key Vault is what matters — feel free to put all four in
Key Vault too for consistency, same pattern as above.)

Also check that soterlive1's SQL Server firewall allows this App
Service's outbound traffic — either enable "Allow Azure services and
resources to access this server" on the SQL Server's networking page,
or add its outbound IPs explicitly:

  az webapp show -g $RESOURCE_GROUP -n $APP_NAME --query outboundIpAddresses -o tsv
EOF
