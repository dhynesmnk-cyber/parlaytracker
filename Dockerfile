# One image for the web app, the worker and the migrations (SPEC.md section 12).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY parlaytracker ./parlaytracker
RUN pip install .
COPY alembic.ini ./
COPY migrations ./migrations
COPY .streamlit ./.streamlit

RUN useradd --system --uid 10001 --home-dir /app parlaytracker
USER parlaytracker

EXPOSE 8501
# deploy/docker-compose.yml sets the command for each service; this default runs the web app.
CMD ["streamlit", "run", "parlaytracker/app/main.py", "--server.address=0.0.0.0", "--server.port=8501"]
