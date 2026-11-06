FROM python:3.12-slim
WORKDIR /app
COPY . /app
ENV PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
# 用法（宿主机 cron 每 10 分钟触发一次）：
#   docker run --rm --env-file .env -v $PWD/state:/app/state -v $PWD/archive:/app/archive radar python -m radar run
ENTRYPOINT []
CMD ["python", "-m", "radar", "run"]
