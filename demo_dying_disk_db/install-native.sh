#!/usr/bin/env bash
# Take over a host that runs a REAL Checkmk agent (incl. TLS-registered agent
# controller) and make it serve the fake db-postgres-01 payload instead.
#
#   Checkmk site ⇄ TLS ⇄ cmk-agent-ctl (real) → systemd socket
#       → /usr/bin/check_mk_agent (relay, installed here)
#       → serve.py on 127.0.0.1:6557 (systemd unit, installed here)
#
# Idempotent — safe to re-run after agent package updates (which overwrite
# the relay) or after copying a new serve.py. Usage, next to serve.py:
#
#   sudo ./install-native.sh          # install/repair everything
#   sudo ./install-native.sh restore  # put the real agent back, stop the demo
#
# Why a relay + the REMOTE read: the controller invokes /usr/bin/check_mk_agent
# via the check-mk-agent@.service socket unit with MK_READ_REMOTE=true and
# writes the remote address into the socket first. A script that doesn't
# consume that line resets the connection ("Connection reset by peer" on
# `cmk-agent-ctl dump`). See the relay below — it mirrors the real agent's
# set_up_remote().
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT="/usr/bin/check_mk_agent"
BACKUP="/usr/bin/check_mk_agent.orig"
UNIT="/etc/systemd/system/cmk-demo-dying-disk.service"
MARKER="Conf12 demo override"
AGENT_PORT="${AGENT_PORT:-6557}"   # 6556 is the controller's own listen port
HTTP_PORT="${HTTP_PORT:-8081}"
RUN_USER="${RUN_USER:-$(stat -c %U "${DIR}/serve.py")}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

if [ "${1:-}" = "restore" ]; then
    [ -f "${BACKUP}" ] && cp -a "${BACKUP}" "${AGENT}" && echo "restored ${AGENT} from ${BACKUP}"
    systemctl disable --now cmk-demo-dying-disk.service 2>/dev/null || true
    rm -f "${UNIT}"; systemctl daemon-reload
    echo "demo removed; real agent back in place"
    exit 0
fi

[ -f "${DIR}/serve.py" ] || { echo "serve.py not found next to this script" >&2; exit 1; }

# --- 1) demo payload server as a systemd service (stdlib-only python) ------ #
cat > "${UNIT}" <<EOF
[Unit]
Description=Conf12 demo: fake db-postgres-01 agent payload (dying disk)
After=network.target

[Service]
Environment=AGENT_PORT=${AGENT_PORT}
Environment=HTTP_PORT=${HTTP_PORT}
Environment=START_STATE=healthy
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 ${DIR}/serve.py
Restart=always
RestartSec=2
User=${RUN_USER}

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now cmk-demo-dying-disk.service
systemctl restart cmk-demo-dying-disk.service   # pick up a freshly copied serve.py

# --- 2) back up the real agent ONCE (never overwrite the backup with a
#        relay from a previous run), then install the relay ----------------- #
if [ -f "${AGENT}" ] && ! grep -q "${MARKER}" "${AGENT}"; then
    [ -f "${BACKUP}" ] || cp -a "${AGENT}" "${BACKUP}"
fi
cat > "${AGENT}" <<EOF
#!/bin/bash
# ${MARKER}: this host impersonates db-postgres-01 (dying-disk story).
# Real agent preserved at ${BACKUP} — restore with:
#   sudo ${DIR}/install-native.sh restore
# Payload: systemd unit cmk-demo-dying-disk.service (serve.py), toggle UI on
# http://localhost:${HTTP_PORT}/admin. The agent controller still does the
# real TLS transport — only the section data is fake.

# When invoked via the systemd socket, the controller first writes the remote
# address into the socket (MK_READ_REMOTE=true) — consume it like the real
# agent does, or closing the socket unread resets the connection.
REMOTE="\${REMOTE_HOST:-\${REMOTE_ADDR:-\${SSH_CLIENT%% *}}}"
[ -z "\${REMOTE}" ] && [ "\${MK_READ_REMOTE}" = "true" ] && read -r -t 5 REMOTE

exec cat < /dev/tcp/127.0.0.1/${AGENT_PORT}
EOF
chmod 755 "${AGENT}"
chown root:root "${AGENT}"

# --- 3) verify the whole chain --------------------------------------------- #
sleep 1
out="$("${AGENT}")"
echo "${out}" | head -4
echo "... $(echo "${out}" | grep -c '^<<<') sections via ${AGENT}"
if command -v cmk-agent-ctl >/dev/null; then
    n="$(cmk-agent-ctl dump 2>/dev/null | grep -c '^<<<' || true)"
    [ "${n}" -gt 0 ] && echo "... ${n} sections via cmk-agent-ctl dump (controller path OK)" \
                     || echo "WARNING: cmk-agent-ctl dump returned nothing — check journalctl -u 'check-mk-agent@*'"
fi
echo "done. toggles: curl localhost:${HTTP_PORT}/admin/degrade|break|heal  (UI: /admin)"
