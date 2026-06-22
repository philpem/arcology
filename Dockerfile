#
# Dockerfile for Arcology
#

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
COPY .git /app/.git
WORKDIR /app

RUN apk add --no-cache git && \
    (git -C /app describe --tags --always --long > /app/VERSION 2>/dev/null \
        || echo "unknown" > /app/VERSION) && \
    apk del git && \
    rm -rf /app/.git

EXPOSE 8000
#CMD ["gunicorn", "-b", "0.0.0.0:8000", "myapp.app"]

VOLUME /var/lib/myapp

COPY Dentrypoint.sh /usr/local/bin
ENTRYPOINT [ "Dentrypoint.sh" ]
