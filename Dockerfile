FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY src ./src
COPY app ./app

RUN pip install . \
    && mkdir -p /workspace/data/processed \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /workspace

USER appuser

EXPOSE 8000 8501
