FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock

COPY . .
RUN useradd --create-home appuser
USER appuser

# Web service by default. The Cloud Run Job overrides the command with:
#   python -m app.cli run
CMD ["sh", "-c", "python -m app.cli migrate && exec uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
