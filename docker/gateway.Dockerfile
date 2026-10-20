# IM 网关服务镜像：仅安装运行时依赖，代码由 compose 挂载或镜像内置。
FROM python:3.13-slim

RUN pip install uv --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

ENV TZ=Asia/Shanghai \
    UV_PROJECT_ENVIRONMENT="/usr/local" \
    UV_COMPILE_BYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

RUN set -ex \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

COPY packages/yuxi-im-gateway/pyproject.toml /app/pyproject.toml
COPY packages/yuxi-im-gateway/uv.lock /app/uv.lock
COPY packages/yuxi-im-gateway/README.md /app/README.md
COPY packages/yuxi-im-gateway/src /app/src

RUN uv sync --no-cache --no-dev --frozen
