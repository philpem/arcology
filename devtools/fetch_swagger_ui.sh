#!/bin/sh
# Download Swagger UI static assets for local development.
#
# In Docker deployments these files are fetched during the image build
# (see Dockerfile).  For local dev outside Docker, run this script once
# after cloning and again whenever you want to update the version.
#
# To pin a specific Swagger UI release, set SWAGGER_VERSION before running:
#   SWAGGER_VERSION=5.18.2 ./devtools/fetch_swagger_ui.sh
#
# Default: latest 5.x release from unpkg.

SWAGGER_VERSION="${SWAGGER_VERSION:-5}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/myapp/static/swagger-ui"

set -e
mkdir -p "$DEST"
echo "Fetching swagger-ui-dist@${SWAGGER_VERSION} -> $DEST"
curl -fsSL "https://unpkg.com/swagger-ui-dist@${SWAGGER_VERSION}/swagger-ui.css" \
     -o "$DEST/swagger-ui.css"
curl -fsSL "https://unpkg.com/swagger-ui-dist@${SWAGGER_VERSION}/swagger-ui-bundle.js" \
     -o "$DEST/swagger-ui-bundle.js"
echo "Done."

# vim: ts=4 sw=4 et
