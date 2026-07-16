# fleet/ — the company-scale server bulk

ONE process (`serve.py`) that synthesizes the ~170 steady-green Linux/Windows
servers of the 300-host company estate (`estate.py up --scale company`) from
the declarative roster in `profiles.py`. The delivery shell
(`deploy/delivery/serve.py`, env `ESTATE_FLEET=1`) spawns it as a single
child and fetches each host's agent output over HTTP.

```bash
python3 serve.py                     # standalone: http://localhost:8102
curl -s localhost:8102/ | jq .count  # roster
curl -s localhost:8102/agent/kvm-01  # one host's full agent output
python3 profiles.py                  # roster arithmetic (counts, VMs/host)
```

Design notes (details in `../CLAUDE.md`, section "Scaling to the 300-host
company"):

- **Never one process per host** at this scale — one process, per-host build
  functions parametrized from `profiles.py`.
- Linux payloads are `hosts/web-frontend-01` parametrized; Windows payloads
  are `hosts/win-dc-01`'s healthy path — every CLAUDE.md parity rule
  (header, pretend TLS, full meminfo, timesyncd, apt sentinel, both lnx_if
  variants, ~30 units) applies to each instance.
- Per-instance variation (uptime, load jitter, MACs, serials, UUIDs, phases)
  is **seeded by the host name** — stable across restarts; counters persist
  in `/var/tmp/cmk-demo-fleet-state.json`.
- VMs are round-robined onto the `kvm-*` hypervisors; each hypervisor's `ps`
  lists its actual guests as qemu processes, and its memory tracks the guest
  RSS sum.
- All fleet hosts are STEADY GREEN. Incidents live in `../hosts/` only.
