#!/bin/sh -e

echo "--- Applying database migrations... ---"
flask db upgrade

echo "--- Creating admin user (if needed)... ---"
flask create-admin

echo "Starting and daemonising Gunicorn..."
gunicorn -b 0.0.0.0:8000 "myapp.app:create_app()" --timeout 300 --workers "${GUNICORN_WORKERS:-4}"

