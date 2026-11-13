FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

# Collect static assets at build time using a dummy secret key
RUN DJANGO_DEBUG=False \
    DJANGO_SECRET_KEY=build-time-placeholder-secret \
    DATABASE_URL=sqlite:///tmp/build.sqlite3 \
    USE_S3_STORAGE=True \
    AWS_ACCESS_KEY_ID=build \
    AWS_SECRET_ACCESS_KEY=build \
    AWS_STORAGE_BUCKET_NAME=build \
    python manage.py collectstatic --noinput && rm -f /tmp/build.sqlite3

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py seed_data && gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 2 --threads 4 --timeout 60"]