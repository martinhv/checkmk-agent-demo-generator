# Meridian Retail demo host: mail-relay-01 (downstream MX unreachable -> deferred mail queue backlog)
FROM python:3.12-slim

WORKDIR /opt/mail-relay-demo
COPY serve.py /opt/mail-relay-demo/serve.py

EXPOSE 6556 8080

ENV AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

ENTRYPOINT ["python3", "-u", "/opt/mail-relay-demo/serve.py"]
