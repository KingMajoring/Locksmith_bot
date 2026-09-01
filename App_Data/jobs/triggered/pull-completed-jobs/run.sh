#!/bin/bash
# Azure WebJob (triggered, CRON-scheduled — see settings.job in this same
# folder). Pulls yesterday's completed jobs from Optimo daily.
set -e

cd /home/site/wwwroot
python manage.py pull_completed_jobs
