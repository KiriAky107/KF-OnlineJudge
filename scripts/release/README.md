# Offline release tooling

Build and operate a KF-OnlineJudge node with locked dependencies and verified
offline materials. The tools cover installation, configuration, HTTPS, maintenance
and cold recovery. Full system and recovery acceptance is still in progress.

For the target platform and installation order, read
[`deploy/release/README.md`](../../deploy/release/README.md).

## Build a release

```sh
python3.13 -m scripts.release build --repo /native/checkout \
  --inputs /private/build-inputs.json --output /private/new-bundle \
  --work /private/new-build-work
```

Start from a clean committed checkout. Output and work directories must be new,
and their filesystem must have the required reserve. The build uses lockfiles and
offline caches, then copies tracked application/deployment sources, fresh locked
production dependencies and separately fingerprinted public runtimes. Backend
source ships with those dependencies; uvx/pip and startup hooks are not runtime
entry points.

The pinned Python 3.13.16 builder compiles checked-hash bytecode from the copied
stdlib, dependencies, backend and delivery sources. It never copies existing
caches. Each read-only `__pycache__/*.cpython-313.pyc` file is included in the full
bundle inventory, must have its source in that inventory, and has its interpreter
header and source hash checked during byte verification. Other cache versions,
timestamp or unchecked caches, orphan caches and caches inside judging images are
refused. Services keep `PYTHONDONTWRITEBYTECODE=1`; they can read these verified
caches without creating runtime files. Original source-only bundles remain valid.

Build inputs contain explicit inventories and expected SHA-256 digests for public
runtimes and the three image catalogs. Keep real input paths and receipts outside
Git. The build receipt records source and manifest digests, file count and byte
total with `installed=false` and `execution_verified=false`.

Use a separately trusted checker and an out-of-band manifest SHA to validate an
incoming bundle. A verified delivery includes `bin/kf-delivery` for later offline
operations. Running an unverified bundle's checker cannot establish trust in that
bundle.

## Authority and service identities

| Operation | Authority |
| --- | --- |
| Build, verify and preflight | Read-only system access |
| File/configuration installation and activation | Root operator |
| Database identity provisioning | Supplied local DB operator |
| Schema and grants | Backend Alembic |
| Application accounts | Backend account CLI |
| Execution state | Coordinator |
| Privileged task preparation and execution | Rust Sandbox |

`policy` validates bounds, `manifest` inventories and verifies files, and `build`
assembles the release. Release tools do not import domain ORM/repositories or
provide a second schema migration path. `scripts/check_architecture.py` checks
the application import boundary. Installation and configuration commands start
no services; the maintenance commands below control their lifecycle.

API/outbox/import share the content-storage identity and use private PID views.
Coordinator and Worker have distinct ordinary identities. `service_bootstrap`
validates Rust credential bytes and writes a private runtime copy with mode 0600
before launching the fixed binary. Tokens and URLs
stay out of arguments and unit text. Python launchers accept fixed modules,
arguments and generated environment fields; inherited debugging, proxy and
provider credentials are omitted. Import also requires a loopback-only network
view and the same admission policy as API.

Every ordinary launch checks UID/GID, cap0, no-new-privileges, node/service cgroup
limits and process file/descriptor/stack limits. The Python services require PID 1
inside their private namespace. Unsupported or ignored isolation settings refuse
startup. Rust Worker has no database credentials. Root factory uses an empty
business environment and gets no database, MQ or platform identity.

## Install files and configuration

`preflight` checks eligibility. `install-files` requires Root, a Root-controlled
verified input tree and the fixed target profile. It installs protected versions,
images and OS identities. Repeat installation checks identical files; unknown
ownership/modes, changed bytes and incomplete staging directories require inspection.

New release files are copied into a private staging directory, synced and fully
read back with their manifest before atomic publication. Each installed runtime
image has its own full readback. Repeat installation checks the release, runtime
images and legacy GNU copy independently; it uses the already checked GNU records
to compare the legacy copy without reading the runtime image a second time.
Payload inventories use one synchronous byte reader in the caller; durable file
transfers retain their four-operation ceiling. Every dispatched read completes and each
file's complete bytes and metadata are checked before publication.

`generate-configuration` reads separate 0600 node and endpoint inputs and writes
a new private batch. API, publisher, import, migration and account operations use
their required DB authority; Worker receives its own MQ/control identity. Backend
URLs use asyncpg. The tool preserves supplied passwords and earlier identities.

The generated single-node policy retains the established language budgets:

| Stage | CPU limit | Wall limit | Memory limit |
| --- | --- | --- | --- |
| C17/C++20 compilation | 30 s | 60 s | 512 MiB |
| Python preparation | 30 s | 60 s | 64 MiB |
| Java compilation | 60 s | 120 s | 512 MiB |
| C17/C++20/Python execution, per case | 5 s | 10 s | 128 MiB |
| Java execution, per case | 5 s | 10 s | 384 MiB |

The job wall limit is 180 s and admission remains one job at a time. The Root
image ceilings cover these limits without changing the task aggregate or its
single slot. Budgets are bound into the configuration receipt and measurement
epoch. Installing a changed policy requires a newly generated, explicitly
attested batch; historical reports keep their original measurement identity.

`install-configuration` validates the entire batch before writing. A batch has a
content-bound `configuration_id`; installing another for the same source retains
the first byte-for-byte. `check-configuration` validates installed bytes,
admission, identity and hardware/kernel/node scheduling bindings.

Run `activate RELEASE --manifest-sha256 SHA --configuration-id BATCH` with the
explicit batch. `activate` requires stopped application units, control ports and
factory socket, and empty task/identity/capture resources. Root journals and
batches are retained. Interruption between pointer writes requires inspection.
`check-active` checks code/configuration selection and the measurement snapshot.

When replacing an existing selection, `activate` fully verifies both releases
and requires identical nonempty Backend migration inventories, runtime images,
platform, toolchains and dependency/contract lock identities. A compatible
software rollback uses the same explicit command with its previously installed
release and configuration batch. An incompatible selection is refused before
either pointer is written. First installation needs no prior selection.
Migration-changing selection is unavailable through this command; it never
downgrades a database or runs Alembic. Normal startup and full readiness remain
separate checks, and activation success does not establish recovery success.

## PostgreSQL and RabbitMQ

### Prepare dependencies

`python3.13 -m scripts.release.dependencies --connections PRIVATE_JSON --output DIRECTORY`
generates fixed loopback configurations and ordinary units for the selected
release. Broker passwords must match the outbox/Worker batch. Its private
definition contains salted hashes, two users without management tags, one
vhost/queue and separate publish/consume permissions. A blank broker must import
these definitions; management and guest/default accounts stay disabled.

PostgreSQL uses SCRAM on local and loopback paths, including the import Unix
socket. Its configuration bounds connections, queries, temporary storage, WAL and
logs. Both dependency services have 512 MiB, zero swap and finite CPU/PIDs/files,
restart and lifetime limits. Memory/disk alarms supplement the hard limits.

### Install snapshots

`python3.13 -m scripts.release.dependency_install DIRECTORY --receipt-sha256 SHA
--erlang-cookie PRIVATE_FILE --cookie-sha256 SHA` installs a snapshot while all
application and dependency services are stopped.

Prepare dedicated ext4 data mounts first, with fixed capacities and
`rw,nosuid,nodev,noexec` options:

| Service | Storage | Ordinary UID/GID |
| --- | --- | --- |
| PostgreSQL | 2 GiB | 1203 |
| RabbitMQ | 512 MiB | 1204 |

The installer does not create, format, resize or mount disks. It can assign an
empty Root-owned mount, allowing only an empty private `lost+found`, to the
dedicated account. Existing data is never recursively reassigned. Accounts must
be locked, have the fixed service homes and lack supplementary authority.

Snapshots live in `/etc/kf-dependencies/RELEASE.CONFIGURATION/`. Each service group
reads its own 0640 files; application configuration remains private. Definition
hashes, permissions and queue budgets must match the selected outbox/Worker batch.
Supply a private 256-bit Erlang cookie. Repeat installation requires the same
ordinary-owned 0400 cookie and leaves its value unchanged.

Only recorded Root-owned unit pointers can be replaced. Old snapshots remain;
loaded/inactive metadata is read back. Before execution, each dependency checks
its UID, cap0, no-new-privileges, process/service/node budgets, private data
ownership and the dedicated filesystem's mount identity and size.

### Initialize PostgreSQL

`python3.13 -m scripts.release.dependency_initialize --password-file PRIVATE_FILE
--password-sha256 SHA` initializes a fresh unrecorded cluster on the selected
snapshot's dedicated mount, with services stopped. Supply a hexadecimal bootstrap
password file. The ordinary PostgreSQL identity runs `initdb` in a bounded,
network-isolated transient unit. systemd supplies the private password file as a
credential, outside arguments and environment values. Local and host
authentication use SCRAM.

The Root-owned receipt records the physical cluster identity and credential
fingerprint. Repeat initialization requires that identity and does not reset its
password. The operator checks the unit result and confirms stop; foreign/residual
units and partial/unrecorded targets are refused. Retain incomplete data for
inspection. This command starts no DB server and migrates no tables.

### Provision database identities

`provision-database-identities` uses locked asyncpg, bounded connections/statements
and a 120-second deadline. It installs fixed independent logins/NOLOGIN groups and
the application database, checks every supplied credential and rejects unexpected
authority. Backend Alembic remains responsible for table DDL and grants.

The migration login owns the database and can inherit/SET the narrow NOLOGIN
function-owner role so Alembic can grant or rename transferred functions. It has
no superuser/create-role/create-db/bypass-RLS rights or ADMIN membership option.
Runtime logins cannot grant/set group authority and never receive that membership.

## Application units and Root factory

`python3.13 -m scripts.release.services generate` renders five ordinary units from
a verified bundle and matching configuration receipt. Users, budgets, restart/stop
limits and pre-start release/batch checks are fixed. The outbox runs the confirmed
publisher module. Service launch code must belong to the selected delivery.

`python3.13 -m scripts.release.service_install DIRECTORY --receipt-sha256 SHA`
installs those units with all application services stopped. Snapshots are retained
in `/etc/kf-onlinejudge/units/RELEASE.CONFIGURATION/`. The installer updates only
known Root-owned pointers and performs a bounded daemon reload. Foreign pointers,
regular files, changed bytes and unknown partial batches refuse replacement.
A stopped repeat installation can finish the same verified batch. Every pre-start
check rejects a pointer left on an unselected batch.

`python3.13 -m scripts.release.factory_service --output DIRECTORY` renders the
separate Root unit after selection and protected configuration checks. The unit
uses the fixed binary, empty business environment, supervisor reservations and
finite lifetime, with the factory's own namespace/filter context.

`factory_service --install DIRECTORY --receipt-sha256 SHA` installs the generated
receipt and unit under `/etc/kf-judge/units/`, preserving earlier snapshots and
checking loaded/inactive systemd metadata. Application units must be stopped;
foreign files/links and unknown partial batches are refused.

On startup, `kf-sandbox prepare-factory` checks the exact node/UID pool, a single
supervisor, the factory lock and empty task/lease/capture resources before creating
cgroup delegation. A boot/inode record identifies only its own empty delegation.
Unknown groups, same-boot loss and conflicting records refuse startup; a new boot
requires empty persistent resources. Preparation retains journals and leases.
Each subsequent execution still needs the full Sandbox isolation check.

## HTTPS entry

`generate-proxy` reads a 0600 profile containing `schema_version: 1`, canonical DNS
`origin`, `certificate_sha256` and `key_sha256`. It generates a new NGINX snapshot
and receipt for the verified release. It accepts fixed upstreams, paths and limits.
Browser API families reach only API loopback; execution-control routes are refused.
ZIP uploads use a separate canonical UUID route.

`python3.13 -m scripts.release.proxy_install --source DIRECTORY
--receipt-sha256 SHA --certificate PRIVATE_CERTIFICATE --private-key PRIVATE_KEY`
installs the selected release/API-origin snapshot and matching unencrypted TLS
material at `/etc/kf-proxy/tls/fullchain.pem` and `key.pem`.

Prepare a 128-MiB private ext4 volume at `/var/lib/kf-onlinejudge/proxy` and the
distinct locked UID/GID 1205 (`kf-proxy`), without supplementary groups. The
installer checks ownership/mode and matching existing TLS fingerprints; it does
not format/mount data volumes, reassign unknown data or rotate keys implicitly.

The ordinary proxy keeps only `CAP_NET_BIND_SERVICE` for 443, no-new-privileges,
private PID visibility and CPU/memory/swap/PIDs/file limits. It has no DB/MQ/control
credentials. Maintenance includes it in start/drain/stop order. Readiness verifies
loopback TLS with the supplied certificate, hostname/leaf identity and API live
response. `/api/v1/health/live` allows GET/HEAD; other health/internal/control
routes remain outside the allowlist. Browser CA trust needs separate verification.

Build-input version 2 specifies the NGINX 1.30.5 catalog. Version 1 supports older
bundles without a proxy. `proxy_runtime` checks every binary/library byte and mode,
excludes TLS/configuration files and ships a separate tree under `payload/proxy`.
Record package signature, version and ELF-closure verification with the inputs.

## Logs

`python3.13 -m scripts.release.journal` installs the fixed KF journal namespace
profile on a preprovisioned 64-MiB ext4 volume at
`/var/log/journal/MACHINE_ID.kf-onlinejudge`. It keeps the default system journal
unchanged and rejects unknown configuration, overrides, old unrecorded data and
partial installs.

The profile uses 32-MiB persistent and 8-MiB runtime rotation budgets, bounded
files/lines and seven-day retention. Its daemon has separate CPU/memory/swap/PIDs
limits. Application, DB/MQ and Root units use this namespace and check the recorded
profile and physical volume before starting.

`check_ready` reads the daemon's process, cgroup and mount state. An observer's
read-only `ProtectSystem` view does not establish daemon writability: the check
matches the daemon's mount device to the protected volume and checks mount and
superblock writability. It rechecks PID, invocation and start time to detect a
changed process, without ptrace/setns or a wider observer PID view.

Production API logs use fixed event names and omit arbitrary library messages,
arguments, paths and exception text. Uvicorn errors use that formatter; installed
API launchers disable access logging so queries are not copied to logs.
Development diagnostics remain separate. Verify real routing, privacy and capacity
on the installed node.

NGINX sends access records to the namespace's local syslog socket. Records contain
only method, status, response byte count and request duration; request paths,
queries, cookies and hostname are omitted.

## Readiness and maintenance

`python3.13 -m scripts.release.node_health` checks admission preconditions in one
fixed Root operator unit. Ordinary broker/factory/import observers have finite
budgets; each checks identity, limits and no-new-privileges/capabilities. The CLI
rejects unknown/concurrent helpers, reads back their stop and accepts no executable,
account, port or command override.

Checks include selected units and effective limits, DB-role/column privileges,
the import owner's fresh policy-bound Unix-socket heartbeat and read-only broker
queue/permissions/vhost queries. Coordinator supplies authenticated execution and
cleanup counts through `/internal/operations/`. Truncated or unconfirmed counts
cannot establish a drain. These internal routes require platform authority and
return no submission IDs, source, archives or credentials.

Factory health is a version-1 authenticated, rate/call-count-bounded observation.
It reports supervision/delegation authorization, config digest, live helpers,
remaining requests and lifetime. It performs no recovery, allocation, ledger write
or limit repair. Request bytes remain unchanged. It reports
`isolation_verified=false` and `execution_gate=required_per_execution`.

Readiness requires confirmed empty execution/cleanup state and enough requests
and lifetime for the maximum job. `--probe-notification` optionally sends one
small confirmed notification for an identifier checked absent by Coordinator. It
reads no body, creates no evaluation and runs no program. Periodic observation
does not publish; probe failures are reported separately.

`python3.13 -m scripts.release.lifecycle --action start|resume|drain|stop|restart`
controls the fixed units and a Root-protected release/batch availability record:

Startup groups read a bounded manager snapshot for their fixed unit IDs. Every
unit still requires its selected unit-file binding, effective profile and
original deadline; missing, duplicate or foreign snapshot entries are refused.
The initial stopped-application check uses the same per-unit bindings in one
snapshot and retains the separate Root cleanup proof.

| Action | Behaviour |
| --- | --- |
| `start` | Requires stopped known application units; opens admission after readiness passes |
| `resume` | Validates known running units after an interrupted operation |
| `drain` | Closes new mutations, waits for owner-confirmed cleanup and leaves DB/MQ running |
| `stop` | Completes the drain, then stops DB/MQ |
| `restart` | Drains before a fresh start |

API returns 503 for an absent, unknown, closed or wrong-bound availability record.
Reads, logout, cancellation and receipt-bound archive uploads keep their existing
identity/lease checks. Both startup paths keep admission closed until the opening
is read back; failed final readiness closes it again.
If a dispatched maintenance helper exits unsuccessfully or reaches its deadline,
the outer operator also closes admission. If that record cannot be written, it
stops only the verified API unit. A refusal before dispatch, such as an already
active helper, does not change admission or stop another operation.

Dispatched failures append one `KF_OPERATOR_DIAGNOSTIC` JSON line to the CLI
error. Its versioned, bounded chain records fixed helper names, failure stages,
exit codes, output sizes and SHA-256 digests. Failed readiness includes only
allowlisted component error codes and at most two dependency helper records.
Raw child output, arguments, credentials and exception text are excluded from
the diagnostic line. Malformed, oversized or duplicate child diagnostic lines
are discarded. This observation does not change service limits, deadlines,
admission checks or cleanup requirements; a failed start remains a failure.

Drain keeps API available to accepted work and polls Coordinator, outbox, import,
broker and factory within a finite deadline. Untruncated owner counts, stopped
process groups, empty tasks/identity/capture resources and an absent factory socket
must agree before quiescence is published. Timeout preserves rows, leases, queues
and replay journals. Root uses its signal/guardian recovery path; import finishes
its current authorized operation before claiming another. The maintenance receipt
records manager operations and cleanup results.

The ordinary outbox process emits `KF_OUTBOX_START_FAILURE` only after a fatal
entrypoint failure. Its bounded JSON record contains an allowlisted startup phase,
exception types, SQLSTATE codes and source basenames/line numbers. Error text,
connection inputs, SQL, environment values and full paths are omitted. The
original nonzero exit and finite service restart policy remain in force; this
record does not establish readiness or explain an unobserved earlier failure.

## Cold backups

Collect the private artifact inventory with
`python3.13 -m kf_oj.infra.artifacts.backup` after owner-confirmed drain, while
PostgreSQL is available. Use the selected API DB/storage identity. A read-only,
repeatable-read transaction checks ready registry objects and source/case digests
with a 64-KiB buffer. The command creates no directory/lock and changes no state.

Run `lifecycle --action stop` and verify processes have exited. A quiescent stop
reuses only this boot's successful selected owner-derived drain receipt and
rechecks application cgroups and Root task cleanup.

`python3.13 -m scripts.release.backup capture
--backup-id bk-<32-lowercase-hex> --references PRIVATE_INVENTORY
--references-sha256 SHA` captures a cold snapshot after checking the selected
batch, owner-derived cut, quiescent admission, empty tasks, stopped service
identities and clean PostgreSQL shutdown.

Prepare a separate Root-owned 0700, 16-GiB ext4 volume at
`/var/lib/kf-onlinejudge/backups`, with `nosuid,nodev,noexec` and no shared mount.
The fixed backup operator has `CAP_DAC_READ_SEARCH`, no-new-privileges, 256 MiB,
zero swap, 32 tasks, 50% CPU, a 256-MiB file limit and a 30-minute deadline. Only
that volume is writable. Backup and restore use `MountFlags=private` so systemd
does not introduce shared propagation in the operator mount namespace.

Backup domains include PostgreSQL, source/case storage, private import staging,
retained releases/images, configuration, replay history and operation receipts.
Every member records bounded digest/size and original identity/mode; copies remain
Root-only. Registry references must match. Links, special files, changing sources,
unknown identities, insufficient space and incomplete cuts are refused. Failed
targets remain unpublished for inspection.

| Limit | Value |
| --- | --- |
| Per snapshot | 7 GiB and 50,000 entries |
| Stored snapshots | At most two |
| Declared retention | Seven days |

`backup verify --backup-id ID
--manifest-sha256 SHA` checks the supplied private receipt and every member.
`backup prune --backup-id EXPIRED_ID --manifest-sha256 SHA
--keep-backup-id NEWER_ID --keep-manifest-sha256 SHA` verifies both inventories,
requires an expired target and a newer unexpired retained copy, then removes only
the named known inventory under the maintenance lock. It retains the last verified
copy and refuses unknown/partial trees. Capacity refusal deletes nothing.

Private v3 backups bind the stopped broker cookie, current DB bootstrap fingerprint
and recovery history. Earlier private v1/v2 prototypes are refused. These private
versions do not alter judging canonical bytes or historical reports. Keep all
backup data and manifests out of Git and CI artifacts.

## Restore to a clean node

Install a verified public bundle on a different clean boot/node. Generate and
select a newly attested batch, then install dependencies with a new broker cookie
on empty provisioned volumes. Keep services stopped and do not initialize a new
cluster over restored data. Use fresh Worker UUID, measurement nonce/epoch,
control tokens and all DB/MQ passwords.

`python3.13 -m scripts.release.restore --recovery-id rr-<32-lowercase-hex>
--backup-id ID --manifest-sha256 SHA` runs the cold data step. Compatibility
requires identical migration bytes, target platform/toolchains and runtime images;
schema-changing upgrades need a separate Backend Alembic procedure.

The operator reads the backup through a read-only mount, checks the private receipt
and every byte, then copies to fixed targets under 512 MiB, zero swap, 32 tasks,
200% CPU and a 30-minute deadline. This uses the existing 200% node ceiling while
all normal services and tasks are stopped. The stopped-state checks precede data
verification and copying and run again before completion. Original identities,
modes and replay IDs are preserved. Its PostgreSQL target is writable; the fresh
RabbitMQ volume is checked through a read-only mount and receives no copied broker state.
Version 4 delegates its file data plane to the selected bundle's `kf-restore-io`
binary inside the existing 512 MiB/200% CPU/32-task restore unit. A single native
process retains two joined worker threads and the verified file witnesses; it
never forks a copy of Python's full inventory. Its only input is the versioned
private stdio protocol, with fixed source/target domains and no command or path
overrides. The binary independently checks Root capabilities, actual unit/node
limits, the read-only backup volume and stopped writers. It starts no programs
or services. Python retains its filesystem error cursors from before directory
creation; the native child holds its own cursors before file writes. Both cursor
sets are synchronized before completion. The original deadlines remain.
After the complete private manifest and its objects have been verified, Python
writes the same sorted batches to a bounded, unlinked 0600 temporary descriptor
in the private recovery directory. It checks the complete spool digest and
stable descriptor identity before sending any batch, then releases the decoded
file inventory and selected bundle manifest. Each streamed batch remains bounded;
its descriptor identity, complete stream digest, count and logical byte total
are checked through EOF. The descriptor is closed on success or failure. This
reduces retained planner objects during copying without changing the native
copy, synchronization, full target readback or final witness checks. The spool
adds no recovery memory, CPU, process or time allowance.
Each batch copies, joins both workers, flushes the affected filesystems, then
reads every target. Final directory sync and stopped/selected-identity checks
precede the native inventory's complete metadata recheck and thread joins.
Transport, copy, byte, sync or identity failure retains the partial target and
returns no successful data receipt. There is no fallback after a native failure.
Version 3 keeps the prior per-file implementation. Older complete bundles remain
eligible for compatible software rollback; new native support requires both its
binary and Python transport module in the bundle.

Each restored file is flushed and read back in full. Within a joined half-batch,
repeated digest/size groups can retain at most 8 MiB of reference bytes per reader.
The first target receives an actual SHA-256 check; every later target is fully
read and compared byte for byte with those verified bytes. Unique and larger
files each receive streaming SHA-256 verification. The reference is discarded
before another group or half-batch; it never certifies a file from metadata alone.
Every target's inode, ownership, mode, link count, size and nanosecond timestamps
must remain unchanged until the final receipt. These observations are local to
one stopped-node recovery and are never reused after an interrupted attempt.
Directory entries are flushed after their children before completion is recorded.
The private v4 inventory accepts up to 98304 logical file/directory entries
and 20 MiB of metadata so retained software/runtime versions are included in full.
The 12 GiB logical, 7 GiB physical and 256 MiB individual-file limits remain;
capture keeps its 256 MiB memory, 50% CPU and original finite deadline. Version 3
keeps its 65536-entry bound. All normalized directory nodes must have a declared
direct parent, which also verifies every ancestor of every file. During restoration,
only this completely verified inventory can reuse its checked target names;
untrusted calls still validate paths. These bounds require actual capture and
recovery validation on the target node; exceeding them is an explicit refusal.
The cold operator, activation and immutable service/dependency/journal profile
installation can reuse a public bundle's complete byte verification within one
synchronous operation. Every file and directory identity, owner, mode, link count,
size and nanosecond mtime/ctime is checked before reuse and before success returns.
Changes are refused. These memory-only proofs are bound to the process, capped at
four bundles, 100006 entries and 16 MiB of metadata, and discarded on return or
failure. Further bundles receive full verification; separate commands start fresh.
Fixed-size regular-file copies can use Linux kernel transfer with bounded chunks.
Unsupported transfer falls back at the same offsets. Source size/EOF, filesystem
durability and full destination byte verification remain required. Version 4
flushes each affected filesystem through error cursors held before writing;
earlier formats retain per-file fsync.
Public bundle inventories read every payload byte with one reader in the caller.
The reader opens without following a final symlink and verifies the
file identity and metadata before and after hashing. The inventory checks those
identities again before returning. The general copy scheduler keeps its default
of four persistent workers taking one item at a time from a locked iterator;
there is no Future for each file or eagerly queued inventory. An explicit single
worker runs inline and checks its deadline before consuming and dispatching each
item. Failure or deadline expiry stops dispatch, and all running calls finish
before an error or result returns. Results retain the input order.
Payload path spelling checks use the same immutable allowlist and may retain at
most 16384 successful, length-bounded strings per process. Custom string subclasses
bypass this cache. Byte reads, filesystem topology, ownership and all metadata
checks remain live; no filesystem or execution result enters the lexical cache.

During one `start`, `resume` or `restart`, the fixed Root maintenance helper reads
the complete bundle once. Its Root startup checks and node-health helpers can
reuse that proof while the same transient maintenance invocation is running.
Consumers require real and effective UID/GID 0 and the exact fixed service
cgroup. A privileged service precheck may retain only Root's group and that
service's fixed group; ordinary service processes still read the complete bytes.
The publishing maintenance helper accepts only Root's own group.
A Root-only systemd runtime directory holds a record of at most 4096 bytes,
binding the full metadata digest to the selected manifest, release, configuration,
boot, MainPID and InvocationID. Each consumer checks the live owner and every
file/directory identity and timestamp before and after use. An eligible malformed,
expired or changed record refuses startup; a missing record requires full bytes.
Inherited proofs cannot be re-exported or retained in a process-local scope.
The maintenance helper removes its exact record before success, and systemd
removes the runtime directory on unit exit, including a killed helper. A final
verification or cleanup failure closes admission. Outside a live maintenance
operation, standalone checks read every byte. Sandbox image verification and
execution isolation remain required
for every actual request. Each consuming process records one fixed journal event
with the manifest digest and maintenance invocation. Normal service and user-program
limits, startup deadlines and the recovery target are unchanged.
PG and MQ start together within the two-CPU node ceiling, then Root starts on its
own before the ordinary service pairs. Root keeps its original preparation limit.
Verified installer and stopped-node recovery inventories can keep at most four
file operations in flight. Each file retains its durability and byte checks;
directory finalization and success receipts wait for all dispatched work. On
failure, queued work is cancelled and running calls are joined before the error
returns. Private partial data is retained for inspection. The original operator
CPU, memory, PID and deadline limits still apply.
The native restore helper keeps two joined threads and partitions each fixed batch
into contiguous groups that minimize the heavier byte count. Ties prefer balanced
file counts. Copy order, synchronization, complete byte readback and final
nine-field witnesses are preserved; no extra worker or batch capacity is added.
Each copy worker keeps at most one read-only source object descriptor within its
joined half-batch. Repeated objects rewind that descriptor instead of reopening
the same path. Every logical copy still checks the protected source, its complete
descriptor/path identity before and after copying, and its exact byte length.
Switching objects closes the old descriptor; synchronization starts only after
both copy groups have returned and closed their source descriptors.

After synchronization and complete byte verification, the native helper asks the
kernel to release clean cached pages for that restored file. Descriptor and path
witnesses are still checked afterwards. This per-file streaming hint avoids
retaining historical file contents in the fixed restore memory budget; it does
not change file bytes, durability, limits or the live filesystem checks.
For an exclusively created destination, its initial descriptor owner and mode
select the necessary changes; an unconditional final descriptor check still
confirms the complete file size, owner, mode and all nine identity fields.
A short read that supplies every declared byte completes hashing or byte equality
without another EOF-only read. Full descriptor/path size and identity checks
still surround the readback; empty files and exact buffer multiples retain an
explicit EOF read. Source EOF checks, synchronization and final all-file witnesses
are unchanged.
Old configuration and maintenance receipts go into inactive
`recovery/ID/history/`. The new boot's Sandbox preparation owns cgroup recreation.
Unknown/nonempty targets and differing existing files are refused; partial restores
remain stopped for inspection.

`data-restored.json` records only byte/identity restoration. Services stay stopped
and DB credential rotation and execution verification remain false.

### Rotate database credentials

`python3.13 -m scripts.release.recovery_database --recovery-id ID
--data-receipt-sha256 SHA --password-file PRIVATE_NEW_BOOTSTRAP
--password-sha256 SHA` requires this boot's selected complete data receipt and a
new 256-bit bootstrap password with a different fingerprint from the backup.
Backup capture requires a complete initialization or recovery marker for the
actual cluster, allowing a restored node to be backed up again. Endpoints come
from current protected configuration.

One ordinary `kf-postgres` temporary unit uses 512 MiB, zero swap, 64 tasks, 100%
CPU, a four-minute deadline, cap0/no-new-privileges and private PID/network/runtime
namespaces. TCP is disabled; the private 0700 socket uses local peer authentication
for this OS identity only. Fixed service logins use SCRAM.

The process checks PostgreSQL settings, roles, memberships and database ownership.
It rotates the five service logins and `postgres` in one transaction, preserves
authority and authenticates every new credential. It changes no schema, grade,
lease or measurement history. SQL/error utility text is suppressed. Confirm
server exit and empty resources before publishing `identity-recovered.json`.

Recovery and startup share a fixed lock. Startup holds a read-only shared lock
through admission and validates linked prepared/data/identity receipts in the
bounded Root-private recovery directories. Missing/incomplete/foreign receipts
keep admission closed, including a committed credential change without confirmed
shutdown. Historical receipts remain valid after reboot or version change but do
not establish current readiness. They stay in inactive history on later restores.

Finish recovery with Backend Alembic verification, new TLS material, service
readiness and actual four-language/history checks on the second VM. The 30-minute
end-to-end target still requires measured acceptance.

## Health query deadlines

Fixed health helpers include interpreter startup in their wall-clock deadline.
Outbox and import queries have 60 seconds each; factory status has 30 seconds and
broker metadata has 45 seconds. Node readiness has 300 seconds. The independent
broker, outbox, import and factory queries run with at most two fixed helpers
in flight; every dispatched helper is joined before readiness returns, including
a failed observation. Results keep their existing validation and failure codes.
Each ordinary helper has one CPU within the node's two-CPU ceiling; its memory,
process and privilege limits remain fixed. These operator budgets
do not change student execution budgets. Startup opens admission only after all
required readiness queries succeed.
The proxy's `Type=exec` state only confirms its launcher is running. Lifecycle
startup and resume confirm the real pinned HTTPS/API response within the existing
130-second service-confirmation deadline. Only connection refusal, timeouts and
upstream 502/503/504 observations retry within that same deadline. Certificate,
hostname, configuration and response-validation failures still refuse startup;
ordinary health queries remain single observations. No service, node or student
resource limit is increased.


Database authority queries use the fixed local TCP/Unix endpoints with TLS disabled
and a five-second connection deadline. SQL remains read-only with a two-second
statement limit. The identity operator needs EXECUTE on the audited authority
function, with no direct account/session access or unlogged authority entrypoint.

Broker metadata uses one fixed read-only RPC with a five-second remote deadline
and a 30-second CLI process deadline, within the helper's 45-second limit. It reads
queue metadata, vhost limits, usernames/tags and permissions; it does not read
messages or password hashes. Readiness requires exactly the configured classic
queue and budgets, including RabbitMQ's optional `x-queue-type=classic` argument.

The broker helper has a 64 MiB per-file ceiling for Erlang JIT mappings.
[OTP 27.3.4](https://github.com/erlang/otp/blob/OTP-27.3.4/erts/emulator/beam/jit/beam_jit_main.cpp)
starts with a 32 MiB JIT block. Broker JSON output remains limited to 32 KiB; other
operator helpers keep the 16 MiB file ceiling.

## Observe resources and queues

`python3.13 -m scripts.release.node_observation` uses a finite read-only Root cap0,
no-new-privileges helper: 128 MiB, zero swap, 16 tasks, 25% CPU, 360 seconds and one
named unit. It reads memory/PIDs current/peak, CPU throttling, memory events, device
IO and filesystem bytes/inodes without enumerating private content. Existing owners
supply service identities, ACLs, leases/cleanup, broker/outbox/import backlog and
factory capacity. Notification probing stays off.

Output includes source/batch, sample time and duration. Readings cover a bounded
time window, not an atomic backup cut. Missing fields, stopped services and failed
owner queries are explicit incomplete observations. Busy admission can be false
in a complete sample; `isolation_verified` remains false. Keep operational output
outside public API, Git and CI artifacts.

## References

- PostgreSQL: [role membership](https://www.postgresql.org/docs/18/role-membership.html),
  [function ownership](https://www.postgresql.org/docs/18/sql-alterfunction.html),
  [initdb](https://www.postgresql.org/docs/18/app-initdb.html),
  [peer authentication](https://www.postgresql.org/docs/18/auth-peer.html),
  [resources](https://www.postgresql.org/docs/18/runtime-config-resource.html),
  [privilege functions](https://www.postgresql.org/docs/18/functions-info.html).
- RabbitMQ: [limits](https://www.rabbitmq.com/docs/limits),
  [definition import](https://www.rabbitmq.com/docs/definitions),
  [access control](https://www.rabbitmq.com/docs/access-control),
  [CLI](https://www.rabbitmq.com/docs/man/rabbitmqctl.8).
- NGINX: [core](https://nginx.org/en/docs/http/ngx_http_core_module.html),
  [proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
  [TLS module](https://nginx.org/en/docs/http/ngx_http_ssl_module.html),
  [syslog](https://nginx.org/en/docs/syslog.html).
- systemd: [credentials](https://systemd.io/CREDENTIALS/),
  [namespace interface](https://github.com/systemd/systemd/blob/v262/man/systemd.exec.xml),
  [journal configuration](https://github.com/systemd/systemd/blob/v262/man/journald.conf.xml).
- Linux: [proc mountinfo](https://github.com/torvalds/linux/blob/v6.18/fs/proc_namespace.c).

恢复大于 64 MiB 的单对象文件时，复制器仅对已复制的只读源区间发出有界缓存回收建议，避免源和目标页面同时占满原恢复单元。目标仍在原同步屏障后完整读回、逐字节/SHA 校验，并在结束前复核全部文件身份；原内存、CPU、线程、期限和失败保留规则保持不变。
