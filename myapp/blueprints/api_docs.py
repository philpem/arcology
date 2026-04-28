"""
Arcology - API Documentation Blueprint

Serves the OpenAPI 3.0 spec and Swagger UI for the REST API.
Routes are intentionally unauthenticated (same as /api/health).
"""

import json
import os
import yaml
from flask import Blueprint, Response, current_app
from ..extensions import csrf

ROUTENAME = __name__.replace('.', '_')

blueprint = Blueprint(ROUTENAME, __name__, url_prefix='/api')


def init_app(app):
    """Exempt API docs from CSRF protection."""
    csrf.exempt(blueprint)


def _spec_path() -> str:
    """Return the absolute path to doc/openapi.yaml."""
    return os.path.realpath(
        os.path.join(current_app.root_path, '..', 'doc', 'openapi.yaml')
    )


@blueprint.route('/openapi.yaml', methods=['GET'])
def openapi_yaml():
    """Serve the raw OpenAPI spec as YAML."""
    path = _spec_path()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            content = fh.read()
    except FileNotFoundError:
        return Response('OpenAPI spec not found', status=404, mimetype='text/plain')
    return Response(content, status=200, mimetype='application/yaml')


@blueprint.route('/openapi.json', methods=['GET'])
def openapi_json():
    """Serve the OpenAPI spec converted to JSON."""
    path = _spec_path()
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            spec = yaml.safe_load(fh)
    except FileNotFoundError:
        return Response('{"error":"OpenAPI spec not found"}', status=404, mimetype='application/json')
    return Response(json.dumps(spec, indent=2), status=200, mimetype='application/json')


_SWAGGER_UI_HTML = '''\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Arcology API Docs</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <style>
    body { margin: 0; }
    #swagger-ui .topbar { display: none; }
  </style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    SwaggerUIBundle({
      url: "/api/openapi.yaml",
      dom_id: "#swagger-ui",
      presets: [SwaggerUIBundle.presets.apis, SwaggerUIBundle.SwaggerUIStandalonePreset],
      layout: "BaseLayout",
      deepLinking: true,
      persistAuthorization: true,
    });
  </script>
</body>
</html>
'''


@blueprint.route('/docs', methods=['GET'])
def swagger_ui():
    """Serve the Swagger UI interactive documentation page."""
    return Response(_SWAGGER_UI_HTML, status=200, mimetype='text/html')

# vim: ts=4 sw=4 et
