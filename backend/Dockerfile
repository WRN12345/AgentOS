FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_RETRIES=5

# 默认官方源；国内网络可在 compose build args 中覆盖为镜像站
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --index-url "${PIP_INDEX_URL}" .

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
