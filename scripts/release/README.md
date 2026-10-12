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

`provision-database-identities` is an explicit local database operator step.
It uses the locked asyncpg dependency, bounded connections/statements and a
120-second overall deadline. It installs only fixed independent logins/NOLOGIN
groups and the application database, verifies every supplied credential and
refuses unexpected existing authority. There is no application table DDL or
automatic account bootstrap: the migration login owns the database without
global superuser/create-role/create-db/bypass-RLS rights; Backend Alembic remains
the only schema/grant migration entry. Runtime logins cannot grant/set group
authority. See [PostgreSQL role membership](https://www.postgresql.org/docs/18/role-membership.html)
and [function ownership](https://www.postgresql.org/docs/18/sql-alterfunction.html).

Always bootstrap validation from a separately trusted tool copy and an out-of-band
manifest SHA. Do not execute a checker supplied inside an unverified bundle to
establish trust in that same bundle. A verified delivery includes `bin/kf-delivery`
and its source for later offline operations. Command success/receipt generation
does not claim Linux execution, working service roles or installation acceptance;
these require the recorded independent VM evidence.
