#!/bin/bash
# Azure WebJob (triggered, CRON-scheduled — see settings.job in this same
# folder). Sends each locksmith their weekly stock check on the day their
# StockCheckSchedule assigns them (see apps/locksmiths/admin.py).
#
# The app is deployed via Oryx's compressed build (output.tar.zst in
# /home/site/wwwroot, which is NOT the app itself — just the artifact),
# extracted at container startup to a per-instance temp dir exposed via
# $APP_PATH (confirmed live: APP_PATH=/tmp/<hash>,
# VIRTUALENVIRONMENT_PATH=$APP_PATH/antenv). WebJobs run in a separate
# process context from the main site and may not inherit that env var,
# so fall back to searching /tmp for it if it's missing — this was a
# real bug: run.sh previously hardcoded `cd /home/site/wwwroot`, which
# never contained manage.py, so this job silently never ran.
set -e

APP_DIR="${APP_PATH:-}"
if [ -z "$APP_DIR" ] || [ ! -f "$APP_DIR/manage.py" ]; then
    APP_DIR=$(find /tmp -maxdepth 1 -type d -exec test -e '{}/manage.py' \; -print 2>/dev/null | head -1)
fi

if [ -z "$APP_DIR" ]; then
    echo "Could not locate the app directory (no manage.py under \$APP_PATH or /tmp)." >&2
    exit 1
fi

"$APP_DIR/antenv/bin/python" "$APP_DIR/manage.py" send_weekly_stock_checks
