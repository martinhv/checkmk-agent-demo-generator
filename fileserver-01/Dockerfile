# Meridian Retail demo host: fileserver-01 (filesystem filling -> df CRIT)
FROM python:3.12-slim

WORKDIR /opt/fileserver-demo
COPY serve.py /opt/fileserver-demo/serve.py

EXPOSE 6556 8080

ENV CMK_HOSTNAME=fileserver-01 \
    AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

ENTRYPOINT ["python3", "-u", "/opt/fileserver-demo/serve.py"]
