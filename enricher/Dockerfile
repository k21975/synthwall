FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY enricher.py config.py ./

# Non-root user for security
RUN useradd -r -u 1000 enricher && chown -R enricher:enricher /app
USER enricher

ENV PYTHONUNBUFFERED=1

CMD ["python3", "enricher.py"]
