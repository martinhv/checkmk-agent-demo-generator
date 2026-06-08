# Meridian Retail demo host: win-dc-01 (Windows Server 2022 AD DC — C: fills up).
# Runs the Windows-FORMAT fake agent from a Linux container (it just writes the
# Windows section text over plaintext TCP; no Windows needed to demo it).
FROM python:3.12-slim

WORKDIR /opt/windc-demo
COPY serve.py /opt/windc-demo/serve.py

EXPOSE 6556 8080

ENV CMK_HOSTNAME=win-dc-01 \
    AGENT_PORT=6556 \
    HTTP_PORT=8080 \
    START_STATE=healthy

ENTRYPOINT ["python3", "-u", "/opt/windc-demo/serve.py"]
