#!/bin/bash
# Azure WebJob (triggered, CRON-scheduled — see settings.job in this same
# folder). Sends each locksmith their weekly stock check on the day their
# StockCheckSchedule assigns them (see apps/locksmiths/admin.py).
set -e

cd /home/site/wwwroot
python manage.py send_weekly_stock_checks
