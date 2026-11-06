FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
RUN python -m venv /app/.venv
COPY requirements.txt .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY banking_agent.py demo.py main.py ./
COPY policies ./policies

RUN groupadd --system banking && useradd --system --create-home --gid banking banking
USER banking
EXPOSE 8088
CMD ["python", "main.py"]

FROM runtime AS test
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt && pip check
COPY pyproject.toml .
COPY tests ./tests
USER banking
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
