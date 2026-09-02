FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace:/workspace/src

WORKDIR /workspace

COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src
COPY app ./app
COPY benchmark/cases ./benchmark/cases
COPY benchmark/ground_truth ./benchmark/ground_truth
COPY experiments/ablation/reports/full-60-4arch-post-pr14-20260831 \
    ./experiments/ablation/reports/full-60-4arch-post-pr14-20260831

RUN pip install . \
    && mkdir -p /workspace/data/processed \
    && useradd --create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /workspace

USER appuser

EXPOSE 8000 8501
