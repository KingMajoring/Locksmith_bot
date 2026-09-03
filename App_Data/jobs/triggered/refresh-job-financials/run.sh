#!/bin/bash
# Azure WebJob (triggered, CRON-scheduled — see settings.job in this same
# folder). Re-checks recent CompletedJob rows still missing net_cost
# against Handl, since Policy_Financial is often entered days after a
# job completes (see refresh_job_financials management command).
#
# Mirrors pull-completed-jobs/run.sh: the app is deployed via Oryx's
# compressed build, extracted at container startup to a per-instance
# temp dir exposed via $APP_PATH — WebJobs run in a separate process
# context and may not inherit that env var, so fall back to searching
# /tmp for it.
set -e

APP_DIR="${APP_PATH:-}"
if [ -z "$APP_DIR" ] || [ ! -f "$APP_DIR/manage.py" ]; then
    APP_DIR=$(find /tmp -maxdepth 1 -type d -exec test -e '{}/manage.py' \; -print 2>/dev/null | head -1)
fi

if [ -z "$APP_DIR" ]; then
    echo "Could not locate the app directory (no manage.py under \$APP_PATH or /tmp)." >&2
    exit 1
fi

"$APP_DIR/antenv/bin/python" "$APP_DIR/manage.py" refresh_job_financials
