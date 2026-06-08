# Meridian Retail demo: piggyback delivery shell.
# Build context is the REPO ROOT (it needs every host's serve.py) — see the
# docker-compose.yml in this directory.
FROM python:3.12-slim

WORKDIR /opt/estate
COPY . /opt/estate

EXPOSE 6556 8080

ENV DELIVERY_HOSTNAME=cmk-demo-gateway \
    AGENT_PORT=6556 \
    HTTP_PORT=8080

ENTRYPOINT ["python3", "-u", "/opt/estate/piggyback-delivery/serve.py"]
