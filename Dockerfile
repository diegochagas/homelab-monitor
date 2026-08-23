FROM python:3.12-alpine

# The bot is pure stdlib — no pip install needed.
WORKDIR /app
COPY homelab_monitor/ /app/homelab_monitor/

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HOST_ROOT=/host

VOLUME ["/data"]

CMD ["python3", "-m", "homelab_monitor.bot"]
