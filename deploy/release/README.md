# Single-node delivery contract

`contract.v1.json` describes the first pre-release installation interface. It is
separate from judging v1/v2 wire formats and never rewrites historical reports.
The reference target is Arch/CachyOS Linux x86_64-v3, glibc 2.44, systemd and cgroup
v2. Images and service libraries are bound to explicit file/catalog fingerprints;
support for other distributions requires a separately verified delivery profile.

The target has at least 4 GiB RAM and 12 GiB installation/data capacity. Judging
and problem-import concurrency remain one each. Configured service and execution
budgets, effective kernel limits and free capacity must also pass preflight; total
RAM alone does not prove enough capacity or verified execution isolation.

Release payloads are directory bundles with a bounded manifest: at most 50,000
single-link regular files, 256 MiB per file, 4 GiB total and 8 MiB metadata. The
manifest SHA-256 is supplied separately by the operator. Links, extra members,
private paths and unexpected file changes are refused. Built payloads and real
configuration never belong in Git. Validation is read-only and does not claim
that any service has been installed, started or verified:

```sh
python3.13 -m scripts.release contract deploy/release/contract.v1.json
python3.13 -m scripts.release verify /path/to/bundle --manifest-sha256 SHA256_FROM_RECEIPT
```

Application versions live below `/opt/kf-onlinejudge/releases`; `current` selects
an explicitly installed version. Private operator/service configuration lives in
`/etc/kf-onlinejudge` and `/etc/kf-judge`, mutable application data under
`/var/lib/kf-onlinejudge`, and protected Sandbox state/images under
`/var/lib/kf-judge`. The delegated task cgroup is separate from the Root factory's
own supervision cgroup. Application processes do not initialize tables on start;
Backend Alembic remains the only migration entry point.

Database login identities are separate for migration, API/outbox, Coordinator,
problem import and local identity operations. Rust Worker and Root factory do not
hold database credentials. Root factory has no MQ/platform/SSH/GitHub identity;
privileged execution remains inside Sandbox. Public and admin browser traffic
share the HTTPS site; execution-control interfaces remain internal.

Upgrade and restore must retain immutable problem versions, submitted source,
audit and original judging reports. A consistent backup cut has a zero confirmed
data-loss target; restoring from already prepared release/backup materials has a
30-minute recovery target, including service health and actual four-language
verification. These are acceptance targets, not results inferred from this file.
Installation/service/restore evidence is required before this pre-release can be
treated as an accepted delivery; the contract itself does not deploy production.
