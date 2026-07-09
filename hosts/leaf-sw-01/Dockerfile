# Meridian Retail demo host: leaf-sw-01 (Cumulus Linux ToR access switch — steady green)
FROM python:3.12-slim

WORKDIR /opt/leaf-sw-demo
COPY serve.py /opt/leaf-sw-demo/serve.py

EXPOSE 6569 8100

ENV AGENT_PORT=6569 \
    HTTP_PORT=8100 \
    STATE_FILE=/var/tmp/cmk-demo-leaf-sw-01.json

ENTRYPOINT ["python3", "-u", "/opt/leaf-sw-demo/serve.py"]
