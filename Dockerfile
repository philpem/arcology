#
# Dockerfile for Arcology
#

# Version stage: derive the version stamp from .git in a throwaway builder so
# the repository history never lands in a layer of the *final* image.  A plain
# `COPY .git` into the final image ships the entire history (extractable by
# anyone who can pull it, and deleting it in a later layer only adds a
# whiteout); doing it in a discarded build stage keeps versioning automatic
# while leaking nothing.  Reuses the same base as the final stage so no extra
# image is pulled.  Requires .git in the build context (see .dockerignore).
FROM python:3-alpine AS version
RUN apk add --no-cache git
COPY .git /src/.git
RUN git --git-dir=/src/.git describe --tags --always --long > /VERSION 2>/dev/null \
        || echo "unknown" > /VERSION

FROM python:3-alpine

COPY requirements.txt /

RUN set -e; \
	apk update \
	&& apk add --virtual .build-deps gcc g++ libffi-dev python3-dev musl-dev \
	&& apk add --no-cache curl libstdc++ libgcc \
	&& pip install --no-cache-dir -r /requirements.txt \
	&& pip install gunicorn \
	&& CC=g++ CXX=g++ LDSHARED="g++ -shared" pip install --no-cache-dir py-tlsh \
	&& apk del .build-deps

# Uncomment this if you want to use sqltap to inspect the SQL query workload
#RUN pip install sqltap

COPY myapp/ /app/myapp/

# Fetch Swagger UI static assets so /api/docs works without internet access.
# To pin a specific release, change @5 to e.g. @5.18.2
RUN mkdir -p /app/myapp/static/swagger-ui && \
    curl -fsSL "https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" \
         -o /app/myapp/static/swagger-ui/swagger-ui.css && \
    curl -fsSL "https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js" \
         -o /app/myapp/static/swagger-ui/swagger-ui-bundle.js

COPY arcology_shared/ /app/arcology_shared/
COPY migrations/ /app/migrations/
COPY doc/ /app/doc/
COPY .flaskenv /app/
WORKDIR /app

# Version stamp, computed automatically in the `version` stage above and copied
# in as just the resulting string — no .git in this (final) image.
COPY --from=version /VERSION /app/VERSION

EXPOSE 8000
#CMD ["gunicorn", "-b", "0.0.0.0:8000", "myapp.app"]

VOLUME /var/lib/myapp

COPY Dentrypoint.sh /usr/local/bin
ENTRYPOINT [ "Dentrypoint.sh" ]
