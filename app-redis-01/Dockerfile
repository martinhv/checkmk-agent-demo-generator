# Meridian Retail demo host: app-redis-01 (maxmemory eviction storm)
FROM python:3.12-slim

WORKDIR /opt/redis-demo
COPY serve.py /opt/redis-demo/serve.py

EXPOSE 6556 8080

ENV CMK_HOSTNAME=app-redis-01 \
    AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

ENTRYPOINT ["python3", "-u", "/opt/redis-demo/serve.py"]
