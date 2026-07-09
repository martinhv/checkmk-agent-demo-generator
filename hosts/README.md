# hosts/ — the agent-based host simulators

One directory per fake host: a stdlib-only `serve.py` that emits realistic
Check_MK agent output over plaintext TCP, plus a Dockerfile/compose for
standalone runs and a README with the Checkmk setup and the demo
choreography. `FLEET.md` (repo root) has the roster, stories, and port map;
`CLAUDE.md` has the engineering rules these hosts follow.

You normally don't start these yourself: `../estate.py up` runs all of them
inside the piggyback delivery shell (`../deploy/piggyback/`), which spawns
each `serve.py` unmodified as a child process — including N steady-green
replicas per class with `--replicas N`.

Standalone (one host, own TCP port, own `/admin` panel):

```bash
cd app-worker-01
docker compose up --build -d
nc 127.0.0.1 6562 | head
open http://localhost:8092/admin
```

The SNMP-simulated network devices are NOT here — they have no serve.py and
no port; see `../snmp/`.
