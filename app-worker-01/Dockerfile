# Meridian Retail demo host: app-worker-01 (memory leak -> OOM -> service flap)
FROM python:3.12-slim

WORKDIR /opt/worker-demo
COPY serve.py /opt/worker-demo/serve.py

EXPOSE 6556 8080

ENV AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

ENTRYPOINT ["python3", "-u", "/opt/worker-demo/serve.py"]
