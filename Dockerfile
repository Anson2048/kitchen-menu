# 国内网络慢可以构建时换镜像：--build-arg PY_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
#   --build-arg APT_MIRROR=mirrors.ustc.edu.cn --build-arg PIP_INDEX=https://mirrors.ustc.edu.cn/pypi/simple
ARG PY_IMAGE=python:3.12-slim
FROM ${PY_IMAGE}
ARG APT_MIRROR=""
ARG PIP_INDEX=""
RUN if [ -n "$APT_MIRROR" ]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi \
 && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir ${PIP_INDEX:+-i $PIP_INDEX} fastapi==0.115.* uvicorn==0.32.* pillow==11.*
WORKDIR /app
COPY app.py index.html ./
COPY tools ./tools
ENV TZ=Asia/Shanghai MENU_DATA_DIR=/data
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
