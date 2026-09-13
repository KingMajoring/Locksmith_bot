#!/bin/bash
# Azure App Service (Linux, Python) startup command.
# Oryx builds the app from requirements.txt automatically before this runs.
set -e

python manage.py migrate --noinput
python manage.py collectstatic --noinput

# --timeout 200: gunicorn's 30s default kills a worker mid-request on
# job completion, where several photo uploads over a locksmith's weak
# mobile signal plus the Handl/Optimo write-back can easily take longer
# than that — the client then sees a hang followed by a 502 (the
# platform reporting the dead upstream worker), and since nothing was
# saved server-side, a page refresh loses all the entered photos/notes/
# signature. 200s stays under Azure App Service's own ~230s front-end
# request ceiling while giving slow uploads real room to finish.
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 200
