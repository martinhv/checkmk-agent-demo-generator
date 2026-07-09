# Meridian Retail demo host: core-gw-01 (datacenter gateway / edge router — steady green)
FROM python:3.12-slim

WORKDIR /opt/core-gw-demo
COPY serve.py /opt/core-gw-demo/serve.py

EXPOSE 6568 8098

ENV AGENT_PORT=6568 \
    HTTP_PORT=8098 \
    STATE_FILE=/var/tmp/cmk-demo-core-gw-01.json

ENTRYPOINT ["python3", "-u", "/opt/core-gw-demo/serve.py"]
