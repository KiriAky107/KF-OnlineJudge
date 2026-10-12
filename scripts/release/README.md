# Offline release tooling

This package is an operator composition tool. `policy` validates delivery bounds,
`manifest` inventories/verifies explicit files, and `build` runs the locked trusted
checkout build and copies only tracked application/deployment sources, fresh
production dependencies and separately fingerprinted public runtime inputs.
It does not import domain ORM/repositories, write application state, implement a
second migration path, execute student code or perform Sandbox privilege work.
`scripts/check_architecture.py` enforces its application import boundary.

All dependency resolution for a release uses existing lockfiles and offline
caches. Backend source is shipped with locked production dependencies rather
than invoking an unpinned isolated wheel build backend. uvx/pip and package
startup path hooks are not delivery runtime entry points. Build inputs specify a
verified public runtime inventory and the three existing image catalogs; an
independent expected SHA-256 binds each input. Real input paths/receipts are local
operator data and do not belong in Git.

```sh
python3.13 -m scripts.release build --repo /native/checkout \
  --inputs /private/build-inputs.json --output /private/new-bundle \
  --work /private/new-build-work
```

The checkout must be clean and committed, build output/work directories new,
and the output filesystem have the required reserve. The resulting receipt
contains the source and manifest fingerprints, file count and byte total, with
`installed=false` and `execution_verified=false`. Installation and service
acceptance remain separate operations. See `deploy/release/README.md` for the
versioned installation contract and authority split.

`preflight` reads installation eligibility only. `install-files` requires root,
a completely root-controlled verified input tree and the fixed target profile;
it installs protected versions/images and bounded OS identities, without table
migrations, application accounts, services or the active-version switch.
Repeated identical files converge; unknown modes/owners, changed bytes or an
incomplete staging directory require operator inspection and are never removed.

`generate-configuration` composes exact public contracts with separately supplied
0600 node and endpoint inputs. It emits a new private directory and a hash-bound
receipt, never prints tokens, never changes a supplied password and never silently
overwrites an earlier identity. API, publisher, import, migration and identity
operations have distinct required database authority; Worker holds only its own
MQ/control identity, Root receives no business credentials. Backend URLs use the
existing asyncpg driver. No uvx/pip or online runtime installation is used.

`install-configuration` validates the entire group before writing any member;
`check-configuration` checks installed bytes, same-source admission, identity and
hardware/kernel/node scheduler bindings. `activate` requires that check and
stopped control ports, no factory socket and no retained task/identity/capture
resources. It atomically selects an installed directory and starts no process;
draining, migrations and actual health remain separate operations. It does not
erase retained journals or release resources lacking cleanup proof.
Every private group also has a content-bound `configuration_id`. Installing a
second group for the same source preserves the first group byte-for-byte.
`activate RELEASE --manifest-sha256 SHA --configuration-id BATCH` selects an
explicit validated batch only after ordinary application units and independent
task resources are stopped. `check-active` checks the protected batch/code
binding and current measurement snapshot, with no liveness/readiness claim.

`provision-database-identities` is an explicit local database operator step.
It uses the locked asyncpg dependency, bounded connections/statements and a
120-second overall deadline. It installs only fixed independent logins/NOLOGIN
groups and the application database, verifies every supplied credential and
refuses unexpected existing authority. There is no application table DDL or
automatic account bootstrap: the migration login owns the database without
global superuser/create-role/create-db/bypass-RLS rights; Backend Alembic remains
the only schema/grant migration entry. Runtime logins cannot grant/set group
authority. The migration login inherits the narrow NOLOGIN function-owner role
and can SET that role, so unchanged Alembic can grant/rename its functions after
ownership transfer. Membership has no ADMIN option, adds no global role-creation
privilege, and is never given to runtime logins. See
[PostgreSQL role membership](https://www.postgresql.org/docs/18/role-membership.html)
and [function ownership](https://www.postgresql.org/docs/18/sql-alterfunction.html).

Always bootstrap validation from a separately trusted tool copy and an out-of-band
manifest SHA. Do not execute a checker supplied inside an unverified bundle to
establish trust in that same bundle. A verified delivery includes `bin/kf-delivery`
and its source for later offline operations. Command success/receipt generation
does not claim Linux execution, working service roles or installation acceptance;
these require the recorded independent VM evidence.

`generate-proxy` is a separate, offline composition entry. Its private 0600
profile has exactly `schema_version: 1`, one canonical HTTPS DNS `origin`,
`certificate_sha256` and `key_sha256`. TLS inputs are independently supplied
later at `/etc/kf-onlinejudge/tls/fullchain.pem` and `key.pem`; the generator
does not read, create or verify a private key. It emits a new private NGINX
configuration and receipt tied to the verified release. No arbitrary upstream,
include, path, credentials or resource overrides are accepted. Only browser API
families target the API loopback port; execution-control paths are refused. ZIP
uploads have their own canonical UUID route. Syntax, actual TLS/stream limits,
static routes, permissions and service health require subsequent VM acceptance;
the generator explicitly returns those capabilities as unverified.
The separately installed `kf-proxy` worker identity must be distinct from API,
Coordinator, import, judging and database/broker identities. It has no business
credentials; generation alone never creates that account or starts NGINX.

Operator responsibilities are separate: build/verify are read-only with respect
to system state; preflight observes eligibility; file/config installation and
activation require the root operator; DB provisioning requires a separately
supplied local DB operator; schema/grants and first account use the existing
Backend Alembic/account CLIs. Configuration validation never starts or restarts
a service. The later bounded service lifecycle must prove drain/cleanup before
maintenance; none of these entries authorizes a Sandbox isolation bypass or
silently substitutes a healthy result when lifecycle evidence is absent.

Proxy directive references: [NGINX core](https://nginx.org/en/docs/http/ngx_http_core_module.html),
[proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html), and
[TLS module](https://nginx.org/en/docs/http/ngx_http_ssl_module.html).

`python3.13 -m scripts.release.services generate` composes five ordinary system
units from a verified bundle and matching private configuration receipt. The
matching service launch modules must themselves be in that bundle. Rendering
uses fixed users, resource ceilings, finite restart/stop limits and an explicit
release/configuration pre-start binding. It never writes system units, starts a
process, migrates a schema or claims readiness. Actual units, mount/PID/cgroup
views, Unix DB socket access, dependency roles and effective limits require VM
installation/acceptance. Root factory and database/broker lifecycle are separate.

API/outbox/import share the established content-storage identity; individual PID
namespaces prevent same-UID inspection of another service's environment. This
requires the fixed systemd profile and verified kernel namespace support. Rust
Coordinator/Worker use distinct ordinary identities and systemd credentials.
Every ordinary launch checks effective service/node cgroup memory, swap, CPU and
PID limits, file/descriptor/stack limits, UID/GID and absence of capabilities
before exec. The three Python services also require PID 1 in their private PID
namespace; import requires a loopback-only network view. Ignored namespace
directives therefore refuse startup. These checks still require actual VM
verification; they do not attest Sandbox isolation or service readiness.
The import environment includes the same validated admission policy as API.

`service_bootstrap` verifies bounded Rust credential bytes, copies them to a
private service runtime directory with the existing required 0600 mode and then
launches only the fixed ordinary binary. It accepts no student executable,
command override or privileged Sandbox operation. Tokens/URLs are not command
arguments or unit text. The outbox unit uses the existing actual confirmed
publisher module; the old scaffold check entry is not treated as a delivery loop.
Python launches use a fixed module/argument list and only the generated service
environment fields; inherited debugging/proxy/provider credentials are omitted.

`python3.13 -m scripts.release.service_install DIRECTORY --receipt-sha256 SHA`
is the separate root operator step for those five ordinary units. It requires
the selected installed configuration, the canonical configuration receipt from
the generator, and matching service launch/installer bytes in that delivery.
All app units must be stopped. It stores immutable batches under the protected
`/etc/kf-onlinejudge/units/RELEASE.CONFIGURATION/` directory, then updates only
known root-owned system unit pointers and performs a bounded daemon reload.
Unknown regular files, foreign pointers, changed bytes and unrecorded old batches
refuse replacement. Previously installed batches remain available; interruption
may leave a mixed set of pointers, but each pre-start check rejects an unselected
binding. Repeat installation can finish the same verified batch after stopping
all app units. Loaded/inactive unit readback starts no service and makes no
claim about effective runtime limits, readiness, Sandbox gates or VM acceptance.

`python3.13 -m scripts.release.factory_service --output DIRECTORY` composes the
separate Root factory unit only after selection and protected configuration checks.
It requires matching composition code in that delivery, uses the fixed Root
binary and empty business environment, and preserves the supervisor reservations
and finite lifetime. It does not inherit the ordinary service namespace/filter
flags. Its pre-start `kf-sandbox prepare-factory` performs privileged cgroup
preparation inside Sandbox: the exact node/UID pool, single supervisor process,
empty task/lease/capture resources and factory lock are required. A retained
boot/inode record recognizes only its own empty delegation; an unknown group,
same-boot loss or conflicting record refuses. New-boot recreation requires empty
persistent resources. No journal/lease is deleted and preparation grants no
execution permit. Neither composition nor lexical tests are Root/kernel/service
acceptance; install, restart, failure recovery and actual execution remain VM-only.
The separate operator step `factory_service --install DIRECTORY --receipt-sha256 SHA`
requires that generated receipt, the selected protected configuration and matching
installer bytes. It keeps immutable Root-unit batches under `/etc/kf-judge/units/`,
updates only recorded Root-owned pointers with all app units stopped, and checks
loaded/inactive systemd metadata. Foreign files/links and partial unknown batches
are refused, old batches remain retained. This step neither starts the factory nor
prepares its cgroup; only the later bounded startup invokes Sandbox preparation.

`python3.13 -m scripts.release.dependencies --connections PRIVATE_JSON --output DIRECTORY`
composes the fixed loopback PostgreSQL/RabbitMQ configurations and ordinary units
from the selected release. Broker passwords must be identical to the selected
outbox/Worker batch. The private definition file contains salted password hashes,
two users without management tags, one vhost/queue and separate publish/consume
permissions. Definitions import is required on a blank broker; no guest account
or default password is configured. The management plugin remains disabled.
PostgreSQL local/loopback paths both use SCRAM, including the Unix import socket.
Configuration includes bounded connections, query/temp budgets and log controls.

Both dependency units reserve 512 MiB, zero swap and bounded CPU/PIDs/files,
restart and lifetime. The receipt requires separate 2-GiB PostgreSQL and 512-MiB
broker storage mounts. These mounts are not created or checked by composition.
RabbitMQ memory/disk alarms and PostgreSQL WAL/temp settings are not total hard
storage/memory guarantees. Required real filesystem/cgroup/queue/auth readbacks,
account ownership, cluster initialization, cookie/definition permissions and
startup/restart are still explicit installation/VM steps; no service is started.
Dependency pre-start checks require the selected release/batch and read back the
ordinary UID, zero capabilities, no-new-privileges, process/cgroup/node budgets,
private data ownership, a separate data filesystem mount and its actual size.
A parent filesystem or an alarm setting therefore cannot silently pass as the
required dedicated hard storage capacity. Config parsing alone does not verify
these conditions or broker semantics on an already-initialized node.

Dependency directive references: [PostgreSQL resources](https://www.postgresql.org/docs/18/runtime-config-resource.html),
[RabbitMQ limits](https://www.rabbitmq.com/docs/limits),
[definition import](https://www.rabbitmq.com/docs/definitions),
and [RabbitMQ access control](https://www.rabbitmq.com/docs/access-control).

`python3.13 -m scripts.release.dependency_install DIRECTORY --receipt-sha256 SHA
--erlang-cookie PRIVATE_FILE --cookie-sha256 SHA` installs the selected dependency
snapshot with all app/dependency services stopped. It requires already provisioned
dedicated ext4 mounts at the two fixed data paths, with their fixed capacity and
`rw,nosuid,nodev,noexec` options. It does not create, format, resize or mount disks.
A root-owned empty mount (apart from an empty private `lost+found`) may be assigned
to its dedicated account; existing data is never recursively reassigned. Ordinary
UID/GID 1203/1204 must be locked, without supplementary authority, and have the
fixed PostgreSQL/RabbitMQ service homes. Conflicting OS accounts are not adopted.

The generated files are installed under `/etc/kf-dependencies/RELEASE.CONFIGURATION/`.
This separate root-owned searchable tree lets each service group read only its
0640 configuration files; the application configuration tree remains private.
The definition file's salted credentials, user privileges and queue budgets must
match the selected outbox/Worker batch. The operator supplies an explicit private
256-bit Erlang cookie; repeat installation requires the same ordinary-owned 0400
cookie and does not implicitly rotate it. No cookie/password bytes are reported.
System unit pointers may replace only recorded root-owned dependency snapshots.
The command retains old batches, verifies loaded/inactive metadata, starts no
service and initializes no database. Cluster initialization, actual effective
limits, broker authorization and startup/readiness still require VM acceptance.

`python3.13 -m scripts.release.dependency_initialize --password-file PRIVATE_FILE
--password-sha256 SHA` is the separate explicit PostgreSQL cluster initialization
step. It requires the selected dependency snapshot, stopped services, the dedicated
storage mount and an operator-supplied hexadecimal bootstrap password file. Only
a fresh unrecorded target is initialized. The fixed ordinary PostgreSQL identity
runs `initdb` in a transient network-isolated unit with node/service/process/disk
budgets checked before execution. systemd passes the password as a private file
credential; its bytes are neither unit arguments nor environment values. Both
local and host authentication use SCRAM; incomplete data is retained for explicit
recovery, not silently deleted or reused. The initializer never starts a database
server or migrates business tables; Alembic and role provisioning remain separate.

A root-owned initialization receipt binds the physical cluster identity and initial
credential fingerprint. Repeat initialization requires that recorded identity and
does not reset a password or claim successful DB authentication. The operator
reads the transient unit result and confirms stop; foreign/residual units and
unrecorded/partial targets are refused. These are implementation interfaces, not
actual VM acceptance. See [PostgreSQL initdb](https://www.postgresql.org/docs/18/app-initdb.html)
and [systemd credentials](https://systemd.io/CREDENTIALS/) for the underlying interfaces.

`python3.13 -m scripts.release.node_health` performs the installed service
admission-precondition checks in a fixed root operator unit. It uses bounded
ordinary broker/factory/import observer units and accepts no executable, account,
port or command override. Each helper checks its effective node/service/process
budgets, identity and no-new-privileges/capability boundary. Unknown or concurrent
units are refused; transient helper stop is read back. This is an operator CLI,
not an additional public HTTP control endpoint, and has no schema/state writer.

The projection checks selected unit pointers and effective identities/limits,
actual DB-role/column privileges, the import owner's fresh policy-bound heartbeat
through its Unix socket, and broker queue semantics/permissions/vhost limits via
ordinary-cookie read-only CLI queries. Coordinator owns the authenticated bounded
execution/cleanup counters; truncated or unconfirmed data never proves a drain.
Its `/internal/operations/` routes require platform authority and are not proxied
publicly. Counters contain no submission IDs, source, archives or credential bytes.

Factory health is a separate version-1 authenticated observation, rate/call-count
bounded for the finite factory lifetime. It reads current supervisor/delegation
authorization, the exact runtime-config digest, live helpers, remaining requests
and lifetime, without recovery, task allocation, ledger writes or limit repair.
The old allocation request bytes are unchanged. Health explicitly reports
`isolation_verified=false` and `execution_gate=required_per_execution`: every real
execution still needs its namespace/root/identity/seccomp/exec acknowledgements.
Admission readiness requires confirmed empty execution/cleanup state and enough
factory requests/lifetime for the configured maximum job. It does not certify
TLS/browser behavior or replace the independent VM's execution acceptance.

The optional explicit `--probe-notification` sends one small confirmed notification
for a fresh identifier checked absent by Coordinator. It reads no message body,
creates no evaluation/grade and starts no program; ordinary periodic observation
does not publish messages. Probe failure is reported separately. Underlying
interfaces: [RabbitMQ CLI](https://www.rabbitmq.com/docs/man/rabbitmqctl.8),
[PostgreSQL privilege functions](https://www.postgresql.org/docs/18/functions-info.html).

`python3.13 -m scripts.release.lifecycle --action start|resume|drain|stop|restart`
is the explicit fixed-unit maintenance entry. Root publishes a protected, versioned
availability record bound to the selected release/configuration. The installed API
checks that record before accepting mutations; an absent, unknown, closed or wrong
binding returns 503. Existing reads, logout, cancellation and receipt-bound archive
uploads retain their ordinary identity/lease checks. Health alone cannot open this
barrier, and service startup does not create schemas, accounts or credentials.

Drain first closes new mutations, keeps the API available to already accepted
work, and polls the Coordinator, outbox, import, broker and factory owners within
a finite deadline. Only their untruncated empty projections permit ordinary
service shutdown. Final owner counts, actual stopped process groups, delegated
tasks/identity/capture resources and absent factory socket must all agree before
quiescence is published. Timeout preserves unfinished rows, leases, queues and
replay journals; no forced grade or cleanup proof is written. `drain` leaves DB/MQ
running, `stop` additionally stops them, and `restart` drains before a fresh start.

Fresh start requires stopped known application units. `resume` explicitly checks
known units that are already running after an interrupted operation; it does not
adopt foreign units or repair unknown resources. Both keep admission closed until
full service preconditions pass and opening is read back. Failed final readiness
closes the barrier again. A bounded Root maintenance receipt records manager
operations and cleanup results. Root factory termination uses a signal notice and
the existing bounded guardian/recovery path; ordinary import termination finishes
its current authorized operation before claiming another. These interfaces require
actual independent-VM lifecycle acceptance and are not installation/safety proof.

`python3.13 -m scripts.release.journal` separately installs a fixed KF journal
namespace profile. It requires a preprovisioned dedicated 64-MiB ext4 mount at
`/var/log/journal/MACHINE_ID.kf-onlinejudge`, with the same private hard-volume
mount rules used for dependencies. It never creates/formats/mounts a disk, changes
the default system journal, adopts old unrecorded log data or starts a daemon.
Unknown configuration/overrides and partial installations are retained and refused.

The profile fixes 32-MiB persistent/8-MiB runtime rotation budgets, bounded files,
lines and seven-day retention. Its namespaced daemon override limits CPU, memory,
swap and PIDs. Rotation settings are not a hard filesystem guarantee; the dedicated
volume must also be observed. The `check_ready` interface reads actual namespace
service/process/cgroup state. Connecting service templates to this profile,
redaction and real retention/capacity behavior remain C+D/F installation acceptance
work; installing the profile alone does not route existing service logs or verify
their privacy. The profile relies on the pinned systemd 262
[namespace interface](https://github.com/systemd/systemd/blob/v262/man/systemd.exec.xml)
and [journal configuration](https://github.com/systemd/systemd/blob/v262/man/journald.conf.xml).
