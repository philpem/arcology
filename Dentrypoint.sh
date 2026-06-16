#!/bin/sh -e

echo "--- Applying database migrations... ---"
flask db upgrade

echo "--- Creating admin user (if needed)... ---"
flask create-admin

echo "Starting Gunicorn..."
# Use the threaded worker class: the app spends most of each request waiting on
# PostgreSQL (and occasionally on a row lock), so the default blocking "sync"
# worker ties up a whole process per in-flight request. Under concurrent
# extraction the worker fleet could saturate the pool and unrelated requests
# would sit in the accept queue past the workers' HTTP read timeout. gthread
# lets each process service several concurrent requests while one is blocked on
# the database.
#
# 'exec' replaces this shell with Gunicorn so it runs as PID 1 and receives
# Docker's SIGTERM directly. Without exec, the shell stays PID 1, never
# forwards the signal, and the container is SIGKILL'd (exit 137) after the
# stop grace period. On SIGTERM Gunicorn stops accepting new connections and
# lets in-flight requests drain for up to --graceful-timeout seconds before
# exiting.
exec gunicorn -b 0.0.0.0:8000 "myapp.app:create_app()" \
    --timeout 300 \
    --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
    --worker-class gthread \
    --workers "${GUNICORN_WORKERS:-4}" \
    --threads "${GUNICORN_THREADS:-8}"

