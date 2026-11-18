# Django 6.0 requires Python >= 3.12.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Week 1 services (web + grpc + simulator) only need requirements/week1.txt.
# Set INSTALL_FULL=1 to install every Week 2-4 dependency (needed for the Streamlit dashboard image).
ARG INSTALL_FULL=0
COPY requirements.txt ./requirements.txt
COPY requirements ./requirements
RUN if [ "$INSTALL_FULL" = "1" ]; then pip install --no-cache-dir -r requirements.txt; \
    else pip install --no-cache-dir -r requirements/week1.txt; fi

COPY . .

RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 8000 8501 50051

# Default command = Django ASGI web server; docker-compose overrides it per service.
CMD ["python", "-m", "daphne", "-b", "0.0.0.0", "-p", "8000", "config.asgi:application"]
