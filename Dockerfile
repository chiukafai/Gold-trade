FROM python:3.10-slim

WORKDIR /app

# 安装系统依赖（curl 用于健康检查，gcc/g++ 用于 pip 安装 numpy/pandas）
RUN apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖（使用阿里云镜像）
# 1) 先升级 pip：python:3.10-slim 自带的旧版 pip 无法解析新版镜像页面
# 2) 加 --default-timeout 和 --retries：容器网络慢/抖动时避免下载中断失败
# 3) 原用清华镜像，但 NAS 容器网络访问其返回空响应（from versions: none），已换阿里云
COPY requirements.txt .
RUN pip install --upgrade pip --default-timeout=120 -i https://mirrors.aliyun.com/pypi/simple/ && \
    pip install --no-cache-dir --default-timeout=120 --retries 5 -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

# 预创建数据目录（确保 DB_PATH 目录存在）
RUN mkdir -p /app/data

# 复制源码
COPY . .

EXPOSE 5002

# 生产环境用 gunicorn 替代 Flask dev server
CMD ["gunicorn", "--bind", "0.0.0.0:5002", "--workers", "2", "--timeout", "30", "app:app"]
