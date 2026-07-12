"""Meridian Retail fleet roster — the steady-green bulk of the 300-host estate.

Declarative host-class profiles consumed by fleet/serve.py (ONE process builds
every host's agent output from these). The counts follow the researched shape
of a real ~300-host estate for a mid-sized online retailer (see FLEET.md):
~10 physical hypervisors carrying ~150 VMs (12-15 per host), roughly 60/40
Linux/Windows among the servers, plus warehouse edge servers. The SNMP side
(switches, firewalls, printers, UPS/PDU, sensors, ...) lives in snmp/, not
here.

Every fleet host is STEADY GREEN: no incident, no toggle. The incident
stories stay unique to the hand-crafted hosts in hosts/ (low noise, one root
cause — the storyline rule).

Schema (dict per class; serve.py applies the defaults):
  prefix        short-name prefix; instances are <prefix>-01 .. -NN
  count         how many instances to stamp out
  os            "linux" | "windows"
  role          folder-taxonomy key used by deploy/cmk_setup.py
  descr         one-line role description (panel / admin JSON)
  parent        short name of the upstream network device (topology)
  site          "dc" | "hq" | "wh1" | "wh2"   (informational)
  ncpu          logical CPUs
  mem_mb        RAM in MiB
  load1         1-min loadavg base (absolute, not per-core)   [linux]
  net_mbs       (rx, tx) MB/s base rates on eth0
  disk          (smart model name, size GB) for /dev/sda      [linux]
  fs            [(mount, size GiB, used fraction), ...] — "/" is implied
                first if not listed                            [linux]
  units         [(unit name, description), ...] role systemd units [linux]
  procs         [(user, vsz kB, rss kB, cmdline), ...] role processes [linux]
  services      [(name, state, description), ...] role Windows services
                (state e.g. "running/auto"); base set added automatically
  win_procs     [(user, vsz kB, ws kB, exe), ...] role processes [windows]
  c_used        C: used fraction                               [windows]
  d_drive       (size GiB, used fraction) or None              [windows]
  uptime_days   (min, max) — each instance picks a value in the range
  mem_profile   (anon_frac, cached_frac, shmem_frac) of RAM    [linux]

VM-to-hypervisor mapping (serve.py:expand_roster): every vm=True host is
distributed round-robin across the hypervisors AT ITS OWN SITE; the VM becomes
a Checkmk CHILD of that hypervisor and shows up as a qemu process in its ps
(cross-checkable realism). Physical iron (hypervisors, the Veeam server) hangs
off an access switch, never the 12-port core. `site` (dc/wh1/wh2) drives both.

`parent=` in the class dicts is vestigial — the network parent is computed in
expand_roster; only `site` matters now.
"""

# Vestigial parent hints — kept only so old class dicts stay valid. The real
# network parent (hypervisor for a VM, access switch for iron) is computed in
# serve.py:expand_roster from `site`; these values are no longer read.
DC = "sw-core-01"
WH1 = "wh1"
WH2 = "wh2"

# --------------------------------------------------------------------------- #
#  Linux VMs — platform + shared infrastructure                                #
# --------------------------------------------------------------------------- #
LINUX_CLASSES = [
    # --- shop platform microservices (the online retailer's own stack) ------
    dict(
        prefix="svc-catalog",
        count=4,
        role="applications",
        descr="Product catalog service (gunicorn)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.8,
        net_mbs=(3.0, 4.5),
        units=[("catalog.service", "Meridian catalog API")],
        procs=[
            (
                "catalog",
                1_310_720,
                480_000,
                "/opt/meridian/catalog/bin/gunicorn -w 4 catalog.wsgi:app",
            ),
            (
                "catalog",
                1_310_720,
                465_000,
                "/opt/meridian/catalog/bin/gunicorn -w 4 catalog.wsgi:app",
            ),
            (
                "catalog",
                1_310_720,
                472_000,
                "/opt/meridian/catalog/bin/gunicorn -w 4 catalog.wsgi:app",
            ),
        ],
    ),
    dict(
        prefix="svc-checkout",
        count=4,
        role="applications",
        descr="Checkout / basket service (gunicorn)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.9,
        net_mbs=(2.5, 2.0),
        units=[("checkout.service", "Meridian checkout API")],
        procs=[
            (
                "checkout",
                1_228_800,
                512_000,
                "/opt/meridian/checkout/bin/gunicorn -w 4 checkout.wsgi:app",
            ),
            (
                "checkout",
                1_228_800,
                505_000,
                "/opt/meridian/checkout/bin/gunicorn -w 4 checkout.wsgi:app",
            ),
        ],
    ),
    dict(
        prefix="svc-order",
        count=4,
        role="applications",
        descr="Order management service (Java)",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        load1=1.1,
        net_mbs=(2.0, 2.4),
        mem_profile=(0.42, 0.28, 0.02),
        units=[("order-svc.service", "Meridian order management")],
        procs=[
            (
                "orders",
                9_400_000,
                5_100_000,
                "java -Xmx6g -jar /opt/meridian/order-svc/order-svc.jar",
            )
        ],
    ),
    dict(
        prefix="svc-account",
        count=3,
        role="applications",
        descr="Customer account service (gunicorn)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.4,
        net_mbs=(1.2, 1.4),
        units=[("account.service", "Meridian account API")],
        procs=[
            (
                "account",
                980_000,
                310_000,
                "/opt/meridian/account/bin/gunicorn -w 3 account.wsgi:app",
            )
        ],
    ),
    dict(
        prefix="svc-inventory",
        count=3,
        role="applications",
        descr="Inventory availability service (Java)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.7,
        net_mbs=(1.8, 1.5),
        mem_profile=(0.40, 0.30, 0.02),
        units=[("inventory-svc.service", "Meridian inventory service")],
        procs=[
            (
                "inventory",
                6_800_000,
                3_400_000,
                "java -Xmx3g -jar /opt/meridian/inventory/inventory-svc.jar",
            )
        ],
    ),
    dict(
        prefix="api-gw",
        count=2,
        role="applications",
        descr="API gateway (nginx + lua)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.5,
        net_mbs=(9.0, 8.5),
        units=[("nginx.service", "A high performance web server and a reverse proxy server")],
        procs=[
            ("root", 8_200, 4_100, "nginx: master process /usr/sbin/nginx"),
            ("www-data", 1_150_000, 160_000, "nginx: worker process"),
            ("www-data", 1_150_000, 158_000, "nginx: worker process"),
            ("www-data", 1_150_000, 162_000, "nginx: worker process"),
            ("www-data", 1_150_000, 156_000, "nginx: worker process"),
        ],
    ),
    dict(
        prefix="shop-search",
        count=3,
        role="applications",
        descr="Product search (Elasticsearch)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.6,
        net_mbs=(4.0, 5.0),
        mem_profile=(0.55, 0.30, 0.01),
        fs=[("/var/lib/elasticsearch", 500, 0.46)],
        units=[("elasticsearch.service", "Elasticsearch")],
        procs=[
            (
                "elasticsearch",
                21_000_000,
                17_200_000,
                "/usr/share/elasticsearch/jdk/bin/java -Xms16g -Xmx16g "
                "org.elasticsearch.bootstrap.Elasticsearch",
            )
        ],
    ),
    dict(
        prefix="shop-media",
        count=3,
        role="applications",
        descr="Media / image resize service",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=1.0,
        net_mbs=(12.0, 18.0),
        fs=[("/srv/media-cache", 400, 0.52)],
        units=[("imgproxy.service", "Meridian image proxy")],
        procs=[("imgproxy", 2_400_000, 900_000, "/usr/local/bin/imgproxy")],
    ),
    dict(
        prefix="queue",
        count=3,
        role="applications",
        descr="Message broker (RabbitMQ)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.6,
        net_mbs=(2.2, 2.2),
        units=[("rabbitmq-server.service", "RabbitMQ Messaging Server")],
        procs=[
            (
                "rabbitmq",
                4_900_000,
                1_350_000,
                "/usr/lib/erlang/erts-13.2/bin/beam.smp -W w -MBas ageffcbf "
                "-- -root /usr/lib/erlang -progname erl -- rabbit",
            )
        ],
    ),
    dict(
        prefix="cache",
        count=3,
        role="applications",
        descr="Object cache (memcached)",
        parent=DC,
        ncpu=2,
        mem_mb=8192,
        load1=0.3,
        net_mbs=(5.0, 6.0),
        mem_profile=(0.55, 0.25, 0.01),
        units=[("memcached.service", "memcached daemon")],
        procs=[
            ("memcache", 4_600_000, 4_200_000, "/usr/bin/memcached -m 4096 -p 11211 -u memcache")
        ],
    ),
    # --- staging copy of the platform (VM sprawl that reads real) -----------
    dict(
        prefix="stg-web",
        count=3,
        role="applications",
        descr="Staging web frontend (nginx)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.15,
        net_mbs=(0.4, 0.35),
        uptime_days=(5, 40),
        units=[("nginx.service", "A high performance web server and a reverse proxy server")],
        procs=[
            ("root", 8_200, 4_000, "nginx: master process /usr/sbin/nginx"),
            ("www-data", 240_000, 45_000, "nginx: worker process"),
            ("www-data", 240_000, 44_000, "nginx: worker process"),
        ],
    ),
    dict(
        prefix="stg-app",
        count=4,
        role="applications",
        descr="Staging app server (gunicorn)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.2,
        net_mbs=(0.3, 0.3),
        uptime_days=(5, 40),
        units=[("meridian-stg.service", "Meridian staging app")],
        procs=[("deploy", 900_000, 280_000, "/opt/meridian/stg/bin/gunicorn -w 2 app.wsgi:app")],
    ),
    dict(
        prefix="stg-db",
        count=1,
        role="databases",
        descr="Staging PostgreSQL (no plugin deployed)",
        parent=DC,
        ncpu=2,
        mem_mb=8192,
        load1=0.3,
        net_mbs=(0.5, 0.4),
        uptime_days=(5, 40),
        mem_profile=(0.20, 0.48, 0.12),
        fs=[("/var/lib/postgresql", 200, 0.31)],
        units=[("postgresql@16-main.service", "PostgreSQL Cluster 16-main")],
        procs=[
            (
                "postgres",
                2_500_000,
                620_000,
                "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main",
            ),
            ("postgres", 2_500_000, 180_000, "postgres: 16/main: checkpointer"),
            ("postgres", 2_500_000, 95_000, "postgres: 16/main: walwriter"),
        ],
    ),
    # --- ERP / BI / WMS back office ------------------------------------------
    dict(
        prefix="erp-app",
        count=3,
        role="applications",
        descr="ERP application server (Java)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.8,
        net_mbs=(1.5, 1.8),
        mem_profile=(0.52, 0.28, 0.02),
        units=[("erp-app.service", "Meridian ERP application server")],
        procs=[("erp", 18_500_000, 13_800_000, "java -Xmx12g -jar /opt/erp/app-server.jar")],
    ),
    dict(
        prefix="erp-db",
        count=2,
        role="databases",
        descr="ERP database (MariaDB, no plugin deployed)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.4,
        net_mbs=(2.0, 3.5),
        mem_profile=(0.55, 0.30, 0.02),
        fs=[("/var/lib/mysql", 800, 0.48)],
        units=[("mariadb.service", "MariaDB 10.11 database server")],
        procs=[
            (
                "mysql",
                19_800_000,
                15_200_000,
                "/usr/sbin/mariadbd --basedir=/usr --datadir=/var/lib/mysql",
            )
        ],
    ),
    dict(
        prefix="bi-app",
        count=1,
        role="applications",
        descr="BI / reporting server (Metabase)",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        load1=0.9,
        net_mbs=(0.8, 1.6),
        mem_profile=(0.45, 0.28, 0.02),
        units=[("metabase.service", "Metabase analytics")],
        procs=[("metabase", 8_900_000, 5_600_000, "java -Xmx4g -jar /opt/metabase/metabase.jar")],
    ),
    dict(
        prefix="bi-db",
        count=1,
        role="databases",
        descr="BI warehouse (PostgreSQL, no plugin deployed)",
        parent=DC,
        ncpu=8,
        mem_mb=65536,
        load1=2.2,
        net_mbs=(1.5, 4.0),
        mem_profile=(0.18, 0.55, 0.14),
        fs=[("/var/lib/postgresql", 2000, 0.57)],
        units=[("postgresql@16-main.service", "PostgreSQL Cluster 16-main")],
        procs=[
            (
                "postgres",
                18_400_000,
                3_100_000,
                "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main",
            ),
            ("postgres", 18_400_000, 2_400_000, "postgres: 16/main: parallel worker"),
            ("postgres", 18_400_000, 890_000, "postgres: 16/main: checkpointer"),
        ],
    ),
    dict(
        prefix="wms-app",
        count=2,
        role="applications",
        descr="Warehouse management system (Java)",
        parent=DC,
        ncpu=8,
        mem_mb=16384,
        load1=1.2,
        net_mbs=(1.8, 1.8),
        mem_profile=(0.48, 0.28, 0.02),
        units=[("wms.service", "Meridian WMS application")],
        procs=[("wms", 10_200_000, 7_400_000, "java -Xmx6g -jar /opt/wms/wms-server.jar")],
    ),
    dict(
        prefix="wms-db",
        count=2,
        role="databases",
        descr="WMS database (PostgreSQL, no plugin deployed)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.5,
        net_mbs=(1.2, 2.6),
        mem_profile=(0.18, 0.52, 0.13),
        fs=[("/var/lib/postgresql", 500, 0.44)],
        units=[("postgresql@16-main.service", "PostgreSQL Cluster 16-main")],
        procs=[
            (
                "postgres",
                9_800_000,
                1_900_000,
                "/usr/lib/postgresql/16/bin/postgres -D /var/lib/postgresql/16/main",
            ),
            ("postgres", 9_800_000, 640_000, "postgres: 16/main: checkpointer"),
            ("postgres", 9_800_000, 210_000, "postgres: 16/main: walwriter"),
        ],
    ),
    # --- Kubernetes (the container platform under the microservices) --------
    dict(
        prefix="k8s-ctl",
        count=3,
        role="applications",
        descr="Kubernetes control-plane node",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        load1=1.0,
        net_mbs=(2.5, 2.5),
        mem_profile=(0.40, 0.30, 0.02),
        fs=[("/var/lib/etcd", 100, 0.18)],
        units=[
            ("kubelet.service", "kubelet: The Kubernetes Node Agent"),
            ("containerd.service", "containerd container runtime"),
        ],
        procs=[
            ("root", 2_900_000, 320_000, "/usr/bin/kubelet --config=/var/lib/kubelet/config.yaml"),
            ("root", 3_400_000, 480_000, "/usr/bin/containerd"),
            ("root", 11_200_000, 1_450_000, "kube-apiserver --etcd-servers=https://127.0.0.1:2379"),
            ("root", 10_800_000, 720_000, "etcd --data-dir=/var/lib/etcd"),
            ("root", 5_600_000, 260_000, "kube-controller-manager"),
            ("root", 4_800_000, 145_000, "kube-scheduler"),
        ],
    ),
    dict(
        prefix="k8s-node",
        count=18,
        role="applications",
        descr="Kubernetes worker node (containerd)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=2.4,
        net_mbs=(6.0, 5.0),
        mem_profile=(0.55, 0.25, 0.02),
        fs=[("/var/lib/containerd", 300, 0.41)],
        units=[
            ("kubelet.service", "kubelet: The Kubernetes Node Agent"),
            ("containerd.service", "containerd container runtime"),
        ],
        procs=[
            ("root", 3_100_000, 340_000, "/usr/bin/kubelet --config=/var/lib/kubelet/config.yaml"),
            ("root", 4_200_000, 610_000, "/usr/bin/containerd"),
            ("root", 1_800_000, 92_000, "/opt/cni/bin/cilium-agent"),
            ("app", 6_400_000, 3_900_000, "containerd-shim-runc-v2 -namespace k8s.io"),
            ("app", 5_100_000, 2_800_000, "containerd-shim-runc-v2 -namespace k8s.io"),
            ("app", 4_400_000, 2_200_000, "containerd-shim-runc-v2 -namespace k8s.io"),
        ],
    ),
    # --- CI/CD + developer infrastructure ------------------------------------
    dict(
        prefix="git",
        count=1,
        role="applications",
        descr="GitLab (omnibus)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.6,
        net_mbs=(2.5, 3.5),
        mem_profile=(0.52, 0.28, 0.04),
        fs=[("/var/opt/gitlab", 500, 0.49)],
        units=[("gitlab-runsvdir.service", "GitLab Runit supervision process")],
        procs=[
            (
                "git",
                4_200_000,
                1_450_000,
                "puma 6.4.0 (unix:///var/opt/gitlab/gitlab-rails/sockets/gitlab.socket)",
            ),
            ("git", 3_800_000, 1_180_000, "sidekiq 7.1.6 queues:default,mailers"),
            ("git", 2_600_000, 480_000, "/opt/gitlab/embedded/bin/gitaly serve"),
            (
                "gitlab-psql",
                1_900_000,
                340_000,
                "/opt/gitlab/embedded/bin/postgres -D /var/opt/gitlab/postgresql/data",
            ),
        ],
    ),
    dict(
        prefix="ci-runner",
        count=6,
        role="applications",
        descr="GitLab CI runner (docker executor)",
        parent=DC,
        ncpu=8,
        mem_mb=16384,
        load1=2.8,
        net_mbs=(3.0, 1.5),
        uptime_days=(3, 30),
        units=[
            ("gitlab-runner.service", "GitLab Runner"),
            ("docker.service", "Docker Application Container Engine"),
        ],
        procs=[
            (
                "root",
                1_900_000,
                110_000,
                "/usr/bin/gitlab-runner run --working-directory /home/gitlab-runner",
            ),
            ("root", 3_600_000, 320_000, "/usr/bin/dockerd -H fd://"),
            ("root", 2_800_000, 190_000, "containerd"),
        ],
    ),
    dict(
        prefix="registry",
        count=1,
        role="applications",
        descr="Container image registry (Harbor)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.5,
        net_mbs=(6.0, 9.0),
        fs=[("/srv/registry", 800, 0.55)],
        units=[("docker.service", "Docker Application Container Engine")],
        procs=[
            ("root", 3_400_000, 300_000, "/usr/bin/dockerd -H fd://"),
            ("10000", 1_600_000, 240_000, "registry serve /etc/registry/config.yml"),
        ],
    ),
    dict(
        prefix="dev",
        count=4,
        role="applications",
        descr="Developer sandbox VM",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        load1=0.4,
        net_mbs=(0.3, 0.2),
        uptime_days=(2, 25),
        units=[("docker.service", "Docker Application Container Engine")],
        procs=[("root", 3_200_000, 280_000, "/usr/bin/dockerd -H fd://")],
    ),
    # --- shared infrastructure -----------------------------------------------
    dict(
        prefix="dns",
        count=3,
        role="infrastructure",
        descr="Internal DNS resolver (BIND 9)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.1,
        net_mbs=(0.4, 0.4),
        uptime_days=(60, 200),
        units=[("named.service", "BIND Domain Name Server")],
        procs=[("bind", 1_450_000, 210_000, "/usr/sbin/named -f -u bind")],
    ),
    dict(
        prefix="proxy",
        count=2,
        role="infrastructure",
        descr="Egress web proxy (Squid)",
        parent=DC,
        ncpu=4,
        mem_mb=8192,
        load1=0.4,
        net_mbs=(7.0, 7.5),
        fs=[("/var/spool/squid", 200, 0.62)],
        units=[("squid.service", "Squid Web Proxy Server")],
        procs=[
            ("proxy", 2_100_000, 950_000, "(squid-1) --kid squid-1 -f /etc/squid/squid.conf"),
            ("root", 120_000, 14_000, "/usr/sbin/squid -f /etc/squid/squid.conf"),
        ],
    ),
    dict(
        prefix="ldap",
        count=2,
        role="infrastructure",
        descr="LDAP directory (OpenLDAP)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.1,
        net_mbs=(0.2, 0.3),
        uptime_days=(90, 300),
        units=[("slapd.service", "OpenLDAP standalone server")],
        procs=[
            ("openldap", 1_800_000, 380_000, "/usr/sbin/slapd -h ldap:/// ldaps:/// -u openldap")
        ],
    ),
    dict(
        prefix="vpn",
        count=1,
        role="infrastructure",
        descr="Remote-access VPN (WireGuard concentrator)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.2,
        net_mbs=(4.0, 4.0),
        units=[("wg-quick@wg0.service", "WireGuard via wg-quick(8) for wg0")],
        procs=[],
    ),
    dict(
        prefix="bastion",
        count=2,
        role="infrastructure",
        descr="SSH jump host",
        parent=DC,
        ncpu=2,
        mem_mb=2048,
        load1=0.05,
        net_mbs=(0.1, 0.1),
        uptime_days=(60, 250),
        procs=[],
    ),
    dict(
        prefix="sftp",
        count=1,
        role="infrastructure",
        descr="B2B file exchange (SFTP)",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.1,
        net_mbs=(1.5, 1.0),
        fs=[("/srv/sftp", 500, 0.38)],
        procs=[],
    ),
    dict(
        prefix="repo",
        count=1,
        role="infrastructure",
        descr="APT mirror / package repo",
        parent=DC,
        ncpu=2,
        mem_mb=4096,
        load1=0.1,
        net_mbs=(1.0, 3.0),
        fs=[("/srv/mirror", 1000, 0.66)],
        units=[("nginx.service", "A high performance web server and a reverse proxy server")],
        procs=[
            ("root", 8_200, 4_000, "nginx: master process /usr/sbin/nginx"),
            ("www-data", 240_000, 40_000, "nginx: worker process"),
        ],
    ),
    dict(
        prefix="log",
        count=4,
        role="infrastructure",
        descr="Central log cluster (OpenSearch)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.8,
        net_mbs=(8.0, 3.0),
        mem_profile=(0.55, 0.30, 0.01),
        fs=[("/var/lib/opensearch", 1500, 0.58)],
        units=[("opensearch.service", "OpenSearch")],
        procs=[
            (
                "opensearch",
                21_500_000,
                17_400_000,
                "/usr/share/opensearch/jdk/bin/java -Xms16g -Xmx16g "
                "org.opensearch.bootstrap.OpenSearch",
            )
        ],
    ),
    dict(
        prefix="monitoring",
        count=1,
        role="infrastructure",
        descr="Checkmk monitoring server (this site)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        load1=1.4,
        net_mbs=(5.0, 2.0),
        mem_profile=(0.35, 0.40, 0.05),
        fs=[("/omd", 500, 0.34)],
        units=[("omd.service", "Checkmk sites"), ("apache2.service", "The Apache HTTP Server")],
        procs=[
            (
                "prod",
                1_400_000,
                260_000,
                "/omd/sites/prod/bin/cmc /omd/sites/prod/var/check_mk/core/config.pb",
            ),
            ("prod", 980_000, 310_000, "/omd/sites/prod/lib/cmc/checkhelper"),
            ("prod", 980_000, 305_000, "/omd/sites/prod/lib/cmc/checkhelper"),
            ("prod", 2_100_000, 480_000, "/usr/sbin/apache2 -k start"),
        ],
    ),
    dict(
        prefix="automation",
        count=1,
        role="infrastructure",
        descr="Ansible / AWX automation controller",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        load1=0.5,
        net_mbs=(0.8, 1.2),
        units=[("docker.service", "Docker Application Container Engine")],
        procs=[
            ("root", 3_300_000, 290_000, "/usr/bin/dockerd -H fd://"),
            ("awx", 2_800_000, 1_100_000, "python3 /usr/bin/awx-manage run_dispatcher"),
        ],
    ),
    dict(
        prefix="vault",
        count=1,
        role="infrastructure",
        descr="Secrets management (HashiCorp Vault)",
        parent=DC,
        ncpu=2,
        mem_mb=8192,
        load1=0.15,
        net_mbs=(0.3, 0.3),
        uptime_days=(60, 200),
        units=[("vault.service", "HashiCorp Vault")],
        procs=[
            ("vault", 2_200_000, 410_000, "/usr/bin/vault server -config=/etc/vault.d/vault.hcl")
        ],
    ),
    dict(
        prefix="ntp",
        count=3,
        role="infrastructure",
        descr="Internal NTP server (chrony)",
        parent=DC,
        ncpu=2,
        mem_mb=2048,
        load1=0.05,
        net_mbs=(0.05, 0.05),
        uptime_days=(120, 380),
        units=[("chrony.service", "chrony, an NTP client/server")],
        procs=[("_chrony", 85_000, 4_800, "/usr/sbin/chronyd -F 1")],
    ),
    # --- warehouse edge servers (behind the WAN routers) ---------------------
    dict(
        prefix="wh1-edge",
        count=3,
        role="infrastructure",
        site="wh1",
        descr="Warehouse 1 edge server (scanner gateway + WMS cache)",
        parent=WH1,
        ncpu=4,
        mem_mb=8192,
        load1=0.5,
        net_mbs=(1.5, 1.5),
        units=[("wms-edge.service", "Meridian WMS edge gateway")],
        procs=[("wms", 2_600_000, 780_000, "java -Xmx2g -jar /opt/wms-edge/edge-gateway.jar")],
    ),
    dict(
        prefix="wh2-edge",
        count=3,
        role="infrastructure",
        site="wh2",
        descr="Warehouse 2 edge server (scanner gateway + WMS cache)",
        parent=WH2,
        ncpu=4,
        mem_mb=8192,
        load1=0.5,
        net_mbs=(1.5, 1.5),
        units=[("wms-edge.service", "Meridian WMS edge gateway")],
        procs=[("wms", 2_600_000, 780_000, "java -Xmx2g -jar /opt/wms-edge/edge-gateway.jar")],
    ),
]

# --------------------------------------------------------------------------- #
#  Physical KVM hypervisors — the iron under the VM fleet. hypervisor=True     #
#  (vm=False): these are not guests; every vm=True host is assigned to a       #
#  hypervisor AT ITS OWN SITE round-robin (serve.py) and shows up as a qemu    #
#  process in that hypervisor's ps output, and becomes its Checkmk child. The  #
#  DC iron is 12 big boxes; each warehouse has one local hypervisor for its    #
#  handful of edge/control VMs (no DC hypervisor reaches across the WAN).      #
# --------------------------------------------------------------------------- #
KVM_CLASSES = [
    dict(
        prefix="kvm",
        count=12,
        site="dc",
        os="linux",
        role="virtualization",
        vm=False,
        hypervisor=True,
        descr="KVM hypervisor (physical, Dell R760)",
        ncpu=48,
        mem_mb=393216,
        load1=6.0,
        net_mbs=(45.0, 45.0),
        mem_profile=(0.62, 0.12, 0.01),
        disk=("Micron 7450 MTFDKBG3T8TFR", 3840),
        fs=[("/var/lib/libvirt/images", 3500, 0.55)],
        uptime_days=(150, 420),
        units=[
            ("libvirtd.service", "Virtualization daemon"),
            ("virtlogd.service", "Virtual machine log manager"),
        ],
        procs=[("root", 2_900_000, 120_000, "/usr/sbin/libvirtd --timeout 120")],
    ),
    # one smaller hypervisor per warehouse, carrying that site's edge/WCS VMs
    dict(
        prefix="wh1-kvm",
        count=1,
        site="wh1",
        os="linux",
        role="virtualization",
        vm=False,
        hypervisor=True,
        descr="Warehouse 1 KVM hypervisor (physical, Dell R660)",
        ncpu=16,
        mem_mb=131072,
        load1=1.5,
        net_mbs=(6.0, 6.0),
        mem_profile=(0.55, 0.12, 0.01),
        disk=("Micron 7450 MTFDKBG1T9TFR", 1920),
        fs=[("/var/lib/libvirt/images", 1500, 0.40)],
        uptime_days=(120, 360),
        units=[
            ("libvirtd.service", "Virtualization daemon"),
            ("virtlogd.service", "Virtual machine log manager"),
        ],
        procs=[("root", 2_900_000, 120_000, "/usr/sbin/libvirtd --timeout 120")],
    ),
    dict(
        prefix="wh2-kvm",
        count=1,
        site="wh2",
        os="linux",
        role="virtualization",
        vm=False,
        hypervisor=True,
        descr="Warehouse 2 KVM hypervisor (physical, Dell R660)",
        ncpu=16,
        mem_mb=131072,
        load1=1.5,
        net_mbs=(6.0, 6.0),
        mem_profile=(0.55, 0.12, 0.01),
        disk=("Micron 7450 MTFDKBG1T9TFR", 1920),
        fs=[("/var/lib/libvirt/images", 1500, 0.40)],
        uptime_days=(120, 360),
        units=[
            ("libvirtd.service", "Virtualization daemon"),
            ("virtlogd.service", "Virtual machine log manager"),
        ],
        procs=[("root", 2_900_000, 120_000, "/usr/sbin/libvirtd --timeout 120")],
    ),
]

# --------------------------------------------------------------------------- #
#  Windows servers                                                             #
# --------------------------------------------------------------------------- #
WINDOWS_CLASSES = [
    dict(
        prefix="win-dc",
        count=2,
        first=2,
        role="windows",
        descr="Active Directory domain controller (Windows Server 2022)",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        c_used=0.48,
        uptime_days=(30, 120),
        services=[
            ("NTDS", "running/auto", "Active Directory Domain Services"),
            ("DNS", "running/auto", "DNS Server"),
            ("Netlogon", "running/auto", "Netlogon"),
            ("Kdc", "running/auto", "Kerberos Key Distribution Center"),
            ("ADWS", "running/auto", "Active Directory Web Services"),
            ("DFSR", "running/auto", "DFS Replication"),
            ("DHCPServer", "running/auto", "DHCP Server"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\SYSTEM", 232800, 58200, "lsass.exe"),
            ("\\\\NT AUTHORITY\\NETWORK SERVICE", 124800, 31200, "dns.exe"),
            (
                "\\\\NT AUTHORITY\\SYSTEM",
                113600,
                28400,
                "Microsoft.ActiveDirectory.WebServices.exe",
            ),
            ("\\\\NT AUTHORITY\\SYSTEM", 79200, 19800, "dfsrs.exe"),
        ],
    ),
    dict(
        prefix="win-file",
        count=4,
        role="windows",
        descr="Windows file server",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        c_used=0.42,
        d_drive=(2048, 0.63),
        services=[
            ("Dfs", "running/auto", "DFS Namespace"),
            ("SearchIndexer", "running/auto", "Windows Search"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\SYSTEM", 98_000, 32_000, "dfssvc.exe"),
            ("\\\\NT AUTHORITY\\SYSTEM", 310_000, 88_000, "SearchIndexer.exe"),
        ],
    ),
    dict(
        prefix="win-print",
        count=1,
        role="windows",
        descr="Print server",
        parent=DC,
        ncpu=2,
        mem_mb=8192,
        c_used=0.39,
        services=[("Spooler", "running/auto", "Print Spooler")],
        win_procs=[("\\\\NT AUTHORITY\\SYSTEM", 182_000, 46_000, "spoolsv.exe")],
    ),
    dict(
        prefix="win-rds",
        count=8,
        role="windows",
        descr="Remote Desktop session host",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        c_used=0.58,
        uptime_days=(5, 40),
        services=[
            ("UmRdpService", "running/demand", "Remote Desktop Services UserMode Port Redirector"),
            ("SessionEnv", "running/demand", "Remote Desktop Configuration"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\SYSTEM", 96_000, 24_000, "svchost.exe"),
            ("MERIDIAN\\user1", 480_000, 220_000, "explorer.exe"),
            ("MERIDIAN\\user2", 460_000, 210_000, "explorer.exe"),
            ("MERIDIAN\\user1", 890_000, 410_000, "OUTLOOK.EXE"),
            ("MERIDIAN\\user3", 1_240_000, 520_000, "EXCEL.EXE"),
        ],
    ),
    dict(
        prefix="win-sql",
        count=5,
        role="databases",
        descr="Microsoft SQL Server 2022",
        parent=DC,
        ncpu=8,
        mem_mb=65536,
        c_used=0.45,
        d_drive=(1024, 0.55),
        services=[
            ("MSSQLSERVER", "running/auto", "SQL Server (MSSQLSERVER)"),
            ("SQLSERVERAGENT", "running/auto", "SQL Server Agent (MSSQLSERVER)"),
            ("SQLWriter", "running/auto", "SQL Server VSS Writer"),
            ("SQLBrowser", "stopped/demand", "SQL Server Browser"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\SYSTEM", 52_000_000, 48_000_000, "sqlservr.exe"),
            ("\\\\NT AUTHORITY\\SYSTEM", 210_000, 62_000, "SQLAGENT.EXE"),
        ],
    ),
    dict(
        prefix="win-app",
        count=22,
        role="applications",
        descr="Windows LOB application server (IIS)",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        c_used=0.51,
        services=[
            ("W3SVC", "running/auto", "World Wide Web Publishing Service"),
            ("WAS", "running/auto", "Windows Process Activation Service"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\SYSTEM", 145_000, 38_000, "inetinfo.exe"),
            ("\\\\IIS APPPOOL\\MeridianApp", 1_450_000, 620_000, "w3wp.exe"),
            ("\\\\IIS APPPOOL\\MeridianApp", 1_380_000, 580_000, "w3wp.exe"),
        ],
    ),
    dict(
        prefix="win-nav",
        count=2,
        role="applications",
        descr="Dynamics 365 Business Central (ERP finance)",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        c_used=0.49,
        services=[
            (
                "MicrosoftDynamicsNavServer$BC230",
                "running/auto",
                "Microsoft Dynamics 365 Business Central Server [BC230]",
            )
        ],
        win_procs=[
            (
                "\\\\NT AUTHORITY\\NETWORK SERVICE",
                6_800_000,
                4_100_000,
                "Microsoft.Dynamics.Nav.Server.exe",
            )
        ],
    ),
    dict(
        prefix="win-wsus",
        count=1,
        role="windows",
        descr="WSUS update server",
        parent=DC,
        ncpu=4,
        mem_mb=16384,
        c_used=0.44,
        d_drive=(500, 0.72),
        services=[
            ("WsusService", "running/auto", "WSUS Service"),
            ("W3SVC", "running/auto", "World Wide Web Publishing Service"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\NETWORK SERVICE", 890_000, 340_000, "WsusService.exe"),
            ("\\\\IIS APPPOOL\\WsusPool", 1_100_000, 480_000, "w3wp.exe"),
        ],
    ),
    dict(
        prefix="win-pki",
        count=1,
        role="windows",
        descr="Enterprise certificate authority",
        parent=DC,
        ncpu=2,
        mem_mb=8192,
        c_used=0.36,
        uptime_days=(60, 200),
        services=[("CertSvc", "running/auto", "Active Directory Certificate Services")],
        win_procs=[("\\\\NT AUTHORITY\\SYSTEM", 96_000, 30_000, "certsrv.exe")],
    ),
    dict(
        prefix="win-veeam",
        count=1,
        role="windows",
        descr="Veeam Backup & Replication server",
        parent=DC,
        ncpu=8,
        mem_mb=32768,
        c_used=0.47,
        d_drive=(4096, 0.68),
        vm=False,
        services=[
            ("VeeamBackupSvc", "running/auto", "Veeam Backup Service"),
            ("VeeamBrokerSvc", "running/auto", "Veeam Broker Service"),
            ("VeeamCatalogSvc", "running/auto", "Veeam Guest Catalog Service"),
            ("VeeamTransportSvc", "running/auto", "Veeam Data Mover Service"),
        ],
        win_procs=[
            ("\\\\NT AUTHORITY\\SYSTEM", 2_400_000, 980_000, "Veeam.Backup.Service.exe"),
            ("\\\\NT AUTHORITY\\SYSTEM", 1_100_000, 420_000, "VeeamAgent.exe"),
        ],
    ),
    dict(
        prefix="wh1-win-wcs",
        count=1,
        role="applications",
        site="wh1",
        descr="Warehouse 1 control system (conveyor/sorter)",
        parent=WH1,
        ncpu=4,
        mem_mb=16384,
        c_used=0.41,
        uptime_days=(60, 250),
        services=[("MeridianWCS", "running/auto", "Meridian Warehouse Control System")],
        win_procs=[("\\\\NT AUTHORITY\\SYSTEM", 1_900_000, 850_000, "Meridian.WCS.Server.exe")],
    ),
    dict(
        prefix="wh2-win-wcs",
        count=1,
        role="applications",
        site="wh2",
        descr="Warehouse 2 control system (conveyor/sorter)",
        parent=WH2,
        ncpu=4,
        mem_mb=16384,
        c_used=0.41,
        uptime_days=(60, 250),
        services=[("MeridianWCS", "running/auto", "Meridian Warehouse Control System")],
        win_procs=[("\\\\NT AUTHORITY\\SYSTEM", 1_900_000, 850_000, "Meridian.WCS.Server.exe")],
    ),
]


def all_classes() -> list[dict]:
    """Every fleet class with os + vm defaults applied."""
    out = []
    for cls in LINUX_CLASSES:
        c = dict(cls)
        c.setdefault("os", "linux")
        c.setdefault("vm", True)
        out.append(c)
    for cls in KVM_CLASSES:
        k = dict(cls)
        k.setdefault("os", "linux")
        k.setdefault("vm", False)
        out.append(k)
    for cls in WINDOWS_CLASSES:
        c = dict(cls)
        c.setdefault("os", "windows")
        c.setdefault("vm", True)
        out.append(c)
    return out


if __name__ == "__main__":
    classes = all_classes()
    lin = sum(c["count"] for c in classes if c["os"] == "linux")
    win = sum(c["count"] for c in classes if c["os"] == "windows")
    vms = sum(c["count"] for c in classes if c.get("vm", True))
    hv = sum(c["count"] for c in classes if c.get("hypervisor"))
    print(
        f"fleet: {lin} linux + {win} windows = {lin + win} hosts "
        f"({vms} VMs on {hv} hypervisors = {vms / hv:.1f}/host)"
    )
