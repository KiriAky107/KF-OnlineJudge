# Single-node delivery

The single-node profile packages the application, four judging runtimes and the
installation tools for offline use. `contract.v1.json` defines the installation
interface. Judging v1/v2 reports keep their existing formats.

Full installation and recovery acceptance is in progress. Validate a release on
an independent test node before using it for production.

## Requirements

| Item | Required profile |
| --- | --- |
| OS | Arch/CachyOS, Linux x86_64-v3, glibc 2.44, systemd 262, cgroup v2 |
| Memory | At least 4 GiB, with enough free capacity for the configured services and tasks |
| Storage | At least 12 GiB filesystem capacity; preflight calculates the required free space |
| Concurrency | One judging job and one problem import |
| Dependencies | PostgreSQL 18.6 and the fixed broker runtime, prepared from verified offline materials |

Other distributions need a separately verified profile. Preflight checks the
effective kernel limits and available capacity as well as nominal RAM and disk
size. Its free-space calculation includes the application, protected runtime
images, the separate GNU legacy image and 4 GiB for mutable data and operations.

## Verify a bundle

Use a separately trusted checker and the manifest digest supplied with the release:

```sh
python3.13 -m scripts.release contract deploy/release/contract.v1.json
python3.13 -m scripts.release verify /path/to/bundle --manifest-sha256 SHA256_FROM_RECEIPT
```

A bundle contains at most 50,000 single-link regular files, 256 MiB per file,
4 GiB in total and 8 MiB of metadata. Verification rejects links, extra members,
private paths and changed bytes. Keep bundles and real configuration outside Git.

Verification is read-only. Installation, running services and execution isolation
each need their own acceptance evidence.

## Directories and authority

| Location | Contents |
| --- | --- |
| `/opt/kf-onlinejudge/releases` | Installed application versions |
| `current` | Explicitly selected application version |
| `/etc/kf-onlinejudge` and `/etc/kf-judge` | Private operator and service configuration |
| `/var/lib/kf-onlinejudge` | Mutable application data |
| `/var/lib/kf-judge` | Protected Sandbox state and runtime images |

Backend Alembic is the only schema migration entry point. Database identities are
separate for migration, API/outbox, Coordinator, problem import and local account
operations. Rust Worker has no database identity; the Root factory has no
database, MQ, platform, SSH or GitHub credentials. Sandbox owns privileged
execution. Public and admin pages share the HTTPS origin; control interfaces stay
internal.

Application services and the task subtree share `kfonlinejudge.slice`, capped at
3840 MiB, zero swap, 384 processes/threads and two CPU equivalents. The remaining
physical memory is reserved for the kernel and minimal OS. Tasks use
`/sys/fs/cgroup/kfonlinejudge.slice/kf-judge-tasks`, separate from the Root
supervisor's cgroup. Read back service and task limits on the target node.

## Prepare configuration

Supply two 0600 single-link JSON files to `generate-configuration`:

| Input | Fields |
| --- | --- |
| Node | `schema_version: 1`, `origin`, `worker_id`, `measurement_nonce` |
| Endpoints | `schema_version: 1`, `database`, `amqp` |

`origin` is one HTTPS origin. `worker_id` is a canonical nonzero UUID.
`measurement_nonce` is null or a fresh operator-generated SHA-256-sized nonce.
The database object has `api`, `coordinator`, `import`, `migration` and `identity`
keys; the broker object has `outbox` and `worker` keys. Supply a different private
credential for every endpoint. Import uses `/run/postgresql`; other control
connections use numeric loopback. The tool checks login names and locations.

The generated admission profile binds C17/C++20, CPython 3.13.16 and OpenJDK
21.0.12.1+1-LTS to verified catalogs and the existing per-language budgets. The
GNU legacy image retains its v1 header and verified executable/library bytes in
a separate directory. Worker, task identities and execution/prefetch/import
slots remain one. Historical reports and runtime identities remain unchanged.

## Select a version

Configuration batches have content-bound `cfg-...` identities. Preparation keeps
earlier batches and does not select a new one. `activate` requires an explicit
`--configuration-id`, a fully validated batch, inactive application units,
stopped control ports and no retained task resources.

Selection writes the code pointer and `/etc/kf-onlinejudge/active.json`. A crash
between those atomic writes can leave a mismatch; stop and inspect it before
starting services. `check-active` rejects inconsistent pointers, changed bytes
and stale hardware bindings. Root journals and earlier batches remain intact.
A stopped stale batch can be retired without accepting it under a changed kernel.

## Performance comparability

Comparability defaults to unavailable (`measurement_epoch=null`). Use
`--attest-measurement` after verifying the node's limits and execution environment.
It binds a fresh nonce, worker, source, images, budgets, CPU/microcode, kernel and
node scheduling snapshot. `check-configuration` rejects a changed binding.

After an environment change, drain execution, install a complete batch with a new
nonce and revalidate before resuming. Earlier reports retain their original
measurements. Configuration validation alone does not prove comparable performance
or execution isolation.

## Installation order

Use `python3.13 -m scripts.release
--help` for the command interface. Prepare the OS dependencies and dedicated data
volumes, then follow this order:

1. Verify the bundle and run preflight.
2. Install files and runtime images.
3. Generate and install a private configuration batch.
4. Activate that batch while services and tasks are stopped.
5. Install the journal profile and selected dependency snapshots.
6. Initialize PostgreSQL, provision DB identities, run Backend Alembic and create
   application accounts; stop the initialization database before installing units.
7. Install ordinary units, the Root factory and the HTTPS snapshot/TLS material.
8. Start the bounded service lifecycle and verify readiness and actual execution.

The tools use offline materials and do not contact a package index.

Startup dispatches PostgreSQL, RabbitMQ and the Root factory together, then the
five ordinary services, then HTTPS after API startup. Each stage waits for every
member's actual manager state and resource/identity checks before continuing.
Every service retains its complete startup verification and its original unit
and observation deadlines, measured from that service's dispatch. Admission stays
closed until both normal readiness checks confirm the complete node. Drain and
stop retain their ordered owner and cleanup checks.

Installer automation can run the same setup CLI entry points in one bounded
Python operation. Use the protected delivery interpreter and its normal Python
environment, as the trusted Root operator:

```python
from scripts.release.setup_session import SetupSession

with SetupSession(maximum_seconds=1800) as setup:
    for command in ("verify", "preflight", "install-files", "install-files"):
        result = setup.invoke(
            "scripts.release",
            [command, "/path/to/bundle", "--manifest-sha256", trusted_manifest_sha256],
        )
        if result.exit_code:
            raise RuntimeError("setup CLI refused; inspect its bounded result")
    # Continue the configuration/dependency/unit steps above with the same CLI
    # module names and explicit argument lists through setup.invoke().
assert setup.completed
```

The session permits at most 32 calls and a deadline up to 2400 seconds. It preserves
an earlier caller alarm. Calls retain their argument parsing, permission checks
and individual exit codes; service startup, judging and private `--inside`
helpers are excluded. Completed steps are not rolled back after a failure.

The first verification of each unchanged bundle reads every byte. Reuse stays
within the same single-threaded process, with complete topology and metadata
checks on every reuse and before the session completes. Staging directories get
an uncached full byte check before publication. Results are provisional until
the context exits successfully; a new session or process starts fresh. Ordinary
standalone CLI invocations keep their existing behavior.

See the [operator command reference](../../scripts/release/README.md) for service,
HTTPS, backup and recovery operations. Upgrades and restores must preserve
immutable problem versions, submitted source, audit and original judging reports.
A confirmed consistent backup cut has a zero data-loss target. Recovery from
prepared release and backup materials has a 30-minute target, including health
and four-language verification. Both targets require measured acceptance results.
