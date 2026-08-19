# syntax=docker/dockerfile:1

# ---- 构建层：用 uv 按锁定依赖安装，并以非 editable 方式安装项目 ----
FROM ghcr.io/astral-sh/uv:0.11.3-python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 先只复制依赖清单，利用 Docker 层缓存；依赖未变时不会重建。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# 再复制源码并安装项目本身（非 editable，运行层无需携带 src）。
COPY src ./src
RUN uv sync --frozen --no-editable

# ---- 运行层：最小 Python 镜像 + 非 root 用户 ----
FROM python:3.12-slim-bookworm
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

# 状态与任务产物写入挂载卷，镜像内不落任何密钥或任务数据。
RUN mkdir -p /data && chown app:app /data

USER app
EXPOSE 8765

# 容器内需要监听 0.0.0.0 才能被宿主机端口映射访问；默认仍以回环地址为准。
ENV REPOPILOT_STATE_DB_PATH=/data/state.sqlite \
    REPOPILOT_QDRANT_URL=http://qdrant:6333 \
    REPOPILOT_API_ALLOW_NONLOCAL=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3)"

CMD ["repopilot-guard", "api", "serve", "--host", "0.0.0.0", "--port", "8765"]
