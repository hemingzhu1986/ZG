FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
# 常驻调度：容器启动后自己按时发送，不需要宿主机 cron
CMD ["python", "-m", "radar", "serve"]
