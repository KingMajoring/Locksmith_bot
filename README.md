# WGTK Ops Tool

Internal tool covering four areas of locksmith operations:

1. **Stock Accuracy** — weekly random stock checks, leakage tracking. *(built)*
2. **Job Completion** — Optimo job failures, timing, mileage. *(next phase)*
3. **Job Costing** — parts + time cost per job, margin, mismatch alarms. *(later phase)*
4. **Panelled Jobs** — jobs handed to non-WGTK locksmiths. *(later phase)*

Build is phased, starting with Stock Accuracy end-to-end before moving on.

## Stack

- Django 5.2 + Postgres (SQLite for local dev)
- Azure AD (Microsoft 365) login via django-allauth, restricted to WGTK
  email domain(s)
- Deployed to Azure (App Service), Postgres on Azure Database for
  PostgreSQL

## Project layout

```
config/                  Django project: settings (base/dev/prod), urls
apps/
  accounts/               Azure AD login + WGTK-domain restriction
  locksmiths/              Shared Locksmith model (synced from Handl)
  integrations/            Handl client (read-only DB access); Optimo
                            client to be added in the Job Completion phase
  stock_accuracy/          Area 1: models, generation/email services,
                            entry queue + variance dashboard, admin
```

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in real values as they become available
python manage.py migrate
python manage.py createsuperuser   # for local admin access without Azure AD
python manage.py runserver
```

With `.env` left mostly blank, the app runs against:
- `MockHandlClient` (apps/integrations/handl.py) — realistic fake stock
  data, so Stock Accuracy can be built/tested without live Handl access.
- the console email backend — sent emails print to the terminal instead
  of actually sending.

## Deploying to Azure

See `deploy/README.md` and `deploy/azure_setup.sh` — a script that
provisions the resource group, Postgres, App Service, and Azure AD app
registration, then deploys the current checkout. Run it on your own
machine with the Azure CLI (this can't reach Azure from a sandboxed
session).

## What's still needed to go live

Status as of the first live deploy (`wgtk-ops-tool` in Azure, resource
group `wgtk-ops-tool-rg`):

- ✅ **Azure hosting**: App Service + Postgres provisioned and deployed.
- ✅ **Azure AD app registration / login**: working — WGTK Microsoft 365
  accounts can sign in and get full admin access.
- ⏳ **Secrets in Key Vault**: `azure_setup.sh` puts secrets straight in
  app settings for the first deploy; run `deploy/keyvault_setup.sh`
  afterwards to migrate them properly (see `deploy/README.md`).
- ⏳ **Handl/Soter DB access**: server/database/username are known
  (`soterlive1.database.windows.net` / `soter_live` / `ExcelReader`, a
  read-only reporting account) — still needed: the password (set via
  Key Vault, never as a plain app setting), confirming the SQL Server
  firewall allows the App Service's outbound IPs, and the actual
  table/column names for stock usage and expected-quantity-by-engineer
  to fill in the two queries in `SQLHandlClient`
  (`apps/integrations/handl.py`, uses `pymssql`).
- ⏳ **Microsoft 365 SMTP**: `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` (or
  swap to Graph API sending if app-password SMTP is locked down), and
  `EMAIL_BACKEND` set to the SMTP backend.
- ⏳ **Locksmith → Handl engineer ID mapping**: populate `Locksmith`
  records (name, `handl_engineer_id`, email) via `/admin/`.
- ⏳ **Stock check schedule**: one `StockCheckSchedule` row per locksmith
  in `/admin/`, picking which weekday they're sent their check.
- ⏳ **Scheduled job**: nothing yet triggers
  `python manage.py send_weekly_stock_checks` daily — needs an Azure
  WebJob, Container App Job, or similar cron-like trigger against the
  App Service.

## Stock Accuracy — how it works

1. `send_weekly_stock_checks` (management command, run daily) checks
   which locksmiths are scheduled for today (`StockCheckSchedule`).
2. For each, `generate_weekly_check` picks their fast-moving pool (top
   usage over `STOCK_CHECK_USAGE_WINDOW_DAYS` from Handl), excludes lines
   checked in the last `STOCK_CHECK_NO_REPEAT_WEEKS` weeks, and randomly
   draws `STOCK_CHECK_LINES_PER_WEEK` (default 10). Expected quantity and
   unit cost are frozen onto the `StockCheckItem` rows at this point.
3. `send_weekly_check` emails an Excel sheet (part code + description
   only — no expected quantity) to the locksmith via Microsoft 365 SMTP.
4. Office staff use the entry queue at `/stock-accuracy/` to type in the
   returned counts once the locksmith replies.
5. Variance/leakage flagging (units, %, £ impact, repeat offender) is
   configured in `/admin/` under **Variance threshold configuration**,
   and shown on the dashboard and per-locksmith report.

## Running tests

```bash
python manage.py test apps
```
