FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Everything not excluded by .dockerignore; historical.json is optional
# (site-specific backfill — present privately, absent in the public repo).
COPY . .

# Non-root user for security (matches enricher convention).
# /data holds the persisted settings; the compose bind mount replaces it in
# production (host dir must be writable by uid 1000 — setup.sh chowns it).
RUN useradd -r -u 1000 webapp && chown -R webapp:webapp /app \
    && mkdir -p /data && chown webapp:webapp /data
USER webapp

ENV PYTHONUNBUFFERED=1
EXPOSE 8050

CMD ["python3", "app.py"]
