#!/bin/bash
# Azure App Service (Linux, Python) startup command.
# Oryx builds the app from requirements.txt automatically before this runs.
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
