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
Preflight requires at least 12 GiB of filesystem capacity and derives required
free bytes from the verified application, protected runtime-image copies and
separate GNU legacy copy, plus 4 GiB of mutable-data/operation reserve. It does
not assume that a nominal 12-GiB filesystem exposes 12 GiB free after metadata.

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
All application services and the separate delegated task subtree live beneath
`kfonlinejudge.slice`: 3840 MiB memory, no swap, 384 processes/threads and two CPU
equivalents. The remaining physical RAM is reserved for the kernel/minimal OS.
The task subtree is `/sys/fs/cgroup/kfonlinejudge.slice/kf-judge-tasks`, a sibling
of the Root factory supervision unit under that same total node budget. Its
installation template does not prove the effective limits; actual service and
task limits are read back in the independent acceptance environment.

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

The node input for `generate-configuration` has exactly `schema_version: 1`,
`origin` (one HTTPS origin), `worker_id` (canonical nonzero UUID), and
`measurement_nonce` (null or a fresh operator-generated SHA-256-sized nonce).
It is a 0600 single-link file. Endpoint input is a separate 0600 JSON object
with exactly `schema_version: 1`, `database` and `amqp`: database keys are
`api`, `coordinator`, `import`, `migration`, `identity`; broker keys are `outbox`
and `worker`. Login names and numeric loopback/socket locations are validated by
the tool. Every endpoint requires its own operator-supplied private credential;
there is no working example password or shared default. Import uses the reachable
`/run/postgresql` Unix socket; all other control connections use numeric loopback.

Generated admission uses the actual verified catalogs for C17/C++20, CPython
3.13.16 and OpenJDK 21.0.12.1+1-LTS and the unchanged public per-language budgets.
The protected GNU legacy image is a separate directory: it retains the original
v1 header and identical verified GNU executable/library bytes, while the three
v2 catalog identities remain untouched. Shared Worker/task UID/GID pool and
execution/prefetch/import slots remain one. No canonical judging report or
historical runtime identity is converted by the installer.

Comparability defaults to unavailable (`measurement_epoch=null`). A trusted
operator may explicitly use `--attest-measurement` only after actual node/service
limits and the intended execution environment have been verified. The attestation
binds a fresh nonce, worker, source, exact images, budgets, CPU/microcode, kernel
and node scheduling snapshot. `check-configuration` refuses an old binding when
those values change; it does not manufacture replacement metrics for old reports.
Before any service resume after such a change, drain execution, generate/install
a new complete private group with a new nonce and independently revalidate the
environment. Raw configuration checks are not performance or isolation evidence.
Configuration batches have independent hash-bound `cfg-...` identities; files
and receipts contain both the source release and batch identifier. A fresh
nonce/identity can therefore be installed for the same source without replacing
an old configuration. Preparation never selects the new batch automatically.
`activate` requires an explicit `--configuration-id`, a fully validated group,
all application units inactive, stopped control ports and no retained task
resources. Root journals and all old batches remain intact. It binds the code
pointer and private batch in `/etc/kf-onlinejudge/active.json`; `check-active`
refuses inconsistent pointers, stale hardware bindings or changed bytes.
Interruption between the two atomic pointer writes requires operator review;
no service starts or claims readiness during that mismatch. A stopped stale
batch can be retired without accepting it for execution under a changed kernel.

The installation entry points are available with `python3.13 -m scripts.release
--help`. Use them in order: verify/preflight, install-files, generate-configuration,
install-configuration, explicit DB identity provision, controlled Backend Alembic
and account CLI operations, then activate and the separately verified bounded
service lifecycle. These operations never contact a package index and do not
install onto a business host on their own. OS dependencies (systemd 262, matching
glibc ABI bytes, PostgreSQL 18.6 and the fixed broker runtime) must already be
present from separately verified offline materials.
