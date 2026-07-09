# Meridian Retail demo host: web-frontend-01 (nginx reverse proxy — steady green)
FROM python:3.12-slim

WORKDIR /opt/web-frontend-demo
COPY serve.py /opt/web-frontend-demo/serve.py

EXPOSE 6556 8080

ENV AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    STATE_FILE=/var/tmp/cmk-demo-web-frontend-state.json

ENTRYPOINT ["python3", "-u", "/opt/web-frontend-demo/serve.py"]
