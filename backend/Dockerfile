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
RUN pip install --index-url "${PIP_INDEX_URL}" ".[dev]"

COPY alembic.ini ./
COPY migrations ./migrations
COPY tests ./tests

# 启动：先迁移到最新版本，再幂等引导初始负责人账号，最后启动 API
CMD ["sh", "-c", "alembic upgrade head && python -m app.scripts.bootstrap && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
