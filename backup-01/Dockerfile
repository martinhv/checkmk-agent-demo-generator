# Meridian Retail demo host: backup-01 (steady-green restic backup host)
FROM python:3.12-slim

WORKDIR /opt/backup-demo
COPY serve.py /opt/backup-demo/serve.py

EXPOSE 6566 8080

ENV CMK_HOSTNAME=backup-01 \
    AGENT_PORT=6566 \
    HTTP_PORT=8080 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "-u", "/opt/backup-demo/serve.py"]
