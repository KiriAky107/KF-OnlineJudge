//! Bounded offline restore data plane. Never starts a program or a service.

mod files;
mod guard;

use files::{Pending, Roots};
use kf_contracts::{
    SchemaVersion,
    restore_io::{self, Error, Reply, ReplyKind, Request, Stage},
};
use std::{
    collections::HashSet,
    io::{self, BufRead, Read, Write},
    sync::{Arc, mpsc},
    thread::{self, JoinHandle},
};

type Result<T> = std::result::Result<T, Error>;

fn now() -> Result<u64> {
    let t = rustix::time::clock_gettime(rustix::time::ClockId::Monotonic);
    u64::try_from(i128::from(t.tv_sec) * 1_000_000_000 + i128::from(t.tv_nsec))
        .map_err(|_| Error::Deadline)
}
fn remaining(deadline: u64) -> Result<()> {
    if now()? >= deadline {
        return Err(Error::Deadline);
    }
    Ok(())
}

enum Job {
    Copy(Vec<restore_io::RestoreFile>, u64),
    Read(Vec<Pending>, u64),
    Stop,
}
enum Completed {
    Copied(Result<Vec<Pending>>),
    Read(Result<Vec<Pending>>),
}
struct Worker {
    sender: mpsc::Sender<Job>,
    receiver: mpsc::Receiver<Completed>,
    join: Option<JoinHandle<()>>,
}
struct Pool {
    workers: Vec<Worker>,
}
impl Pool {
    fn new(roots: Arc<Roots>) -> Result<Self> {
        let mut pool = Self {
            workers: Vec::new(),
        };
        for _ in 0..2 {
            let (sender, jobs) = mpsc::channel();
            let (completed, receiver) = mpsc::channel();
            let roots = Arc::clone(&roots);
            let join = thread::Builder::new()
                .name("restore-io".into())
                .stack_size(1024 * 1024)
                .spawn(move || {
                    while let Ok(job) = jobs.recv() {
                        let result = match job {
                            Job::Copy(rows, deadline) => Completed::Copied(
                                rows.into_iter().map(|r| roots.copy(r, deadline)).collect(),
                            ),
                            Job::Read(rows, deadline) => {
                                Completed::Read(roots.read_group(rows, deadline))
                            }
                            Job::Stop => break,
                        };
                        if completed.send(result).is_err() {
                            break;
                        }
                    }
                })
                .map_err(|_| Error::Filesystem)?;
            pool.workers.push(Worker {
                sender,
                receiver,
                join: Some(join),
            });
        }
        Ok(pool)
    }
    fn materialize(
        &self,
        rows: Vec<restore_io::RestoreFile>,
        roots: &Roots,
        deadline: u64,
    ) -> Result<Vec<Pending>> {
        let size = rows.len().div_ceil(2);
        let mut groups = vec![Vec::new(), Vec::new()];
        for (i, row) in rows.into_iter().enumerate() {
            groups[i / size].push(row);
        }
        for (worker, group) in self.workers.iter().zip(groups) {
            worker
                .sender
                .send(Job::Copy(group, deadline))
                .map_err(|_| Error::Filesystem)?;
        }
        let mut pending = Vec::with_capacity(size * 2);
        let mut error = None;
        // Observe both completions even when the first failed. No sync/readback
        // and no success response can outrun an outstanding copy worker.
        for worker in &self.workers {
            match worker.receiver.recv() {
                Ok(Completed::Copied(Ok(rows))) => pending.extend(rows),
                Ok(Completed::Copied(Err(e))) => {
                    error.get_or_insert(e);
                }
                _ => {
                    error.get_or_insert(Error::Filesystem);
                }
            }
        }
        if let Some(e) = error {
            return Err(e);
        }
        roots.sync(deadline)?;
        let mut groups = vec![Vec::new(), Vec::new()];
        for (i, row) in pending.into_iter().enumerate() {
            groups[i / size].push(row);
        }
        for (worker, group) in self.workers.iter().zip(groups) {
            worker
                .sender
                .send(Job::Read(group, deadline))
                .map_err(|_| Error::Filesystem)?;
        }
        let mut verified = Vec::with_capacity(size * 2);
        for worker in &self.workers {
            match worker.receiver.recv() {
                Ok(Completed::Read(Ok(rows))) => verified.extend(rows),
                Ok(Completed::Read(Err(e))) => {
                    error.get_or_insert(e);
                }
                _ => {
                    error.get_or_insert(Error::Filesystem);
                }
            }
        }
        if let Some(e) = error {
            return Err(e);
        }
        remaining(deadline)?;
        Ok(verified)
    }
}
impl Drop for Pool {
    fn drop(&mut self) {
        for worker in &self.workers {
            let _ = worker.sender.send(Job::Stop);
        }
        for worker in &mut self.workers {
            if let Some(join) = worker.join.take() {
                let _ = join.join();
            }
        }
    }
}

struct Session {
    // Pool drops/joins before the shared descriptors or verified inventory.
    pool: Pool,
    roots: Arc<Roots>,
    deadline: u64,
    expected_files: u32,
    expected_bytes: u64,
    batch_id: u32,
    bytes: u64,
    seen: HashSet<String>,
    verified: Vec<Pending>,
}
impl Session {
    fn open(request: Request) -> Result<Self> {
        let Request::Open {
            backup_id,
            recovery_id,
            expected_files,
            expected_logical_bytes,
            deadline_monotonic_ns,
            ..
        } = request
        else {
            return Err(Error::Protocol);
        };
        remaining(deadline_monotonic_ns)?;
        if deadline_monotonic_ns
            .checked_sub(now()?)
            .is_none_or(|n| n > 1700 * 1_000_000_000)
        {
            return Err(Error::Deadline);
        }
        let roots = Arc::new(Roots::production(&backup_id, &recovery_id)?);
        let pool = Pool::new(Arc::clone(&roots))?;
        Ok(Self {
            pool,
            roots,
            deadline: deadline_monotonic_ns,
            expected_files,
            expected_bytes: expected_logical_bytes,
            batch_id: 0,
            bytes: 0,
            seen: HashSet::with_capacity(expected_files as usize),
            verified: Vec::with_capacity(expected_files as usize),
        })
    }
    fn reply(&self, stage: Stage, error: Error) -> Reply {
        Reply {
            schema_version: SchemaVersion,
            kind: ReplyKind::RestoreIO,
            stage,
            batch_id: self.batch_id,
            verified_files: self.verified.len() as u32,
            verified_logical_bytes: self.bytes,
            execution_verified: false,
            error,
        }
    }
    fn batch(&mut self, batch_id: u32, rows: Vec<restore_io::RestoreFile>) -> Result<()> {
        remaining(self.deadline)?;
        let bytes: u64 = rows.iter().map(|r| r.bytes).sum();
        if batch_id != self.batch_id + 1
            || self.verified.len() + rows.len() > self.expected_files as usize
            || self
                .bytes
                .checked_add(bytes)
                .is_none_or(|n| n > self.expected_bytes)
            || rows.iter().any(|r| !self.seen.insert(r.path.clone()))
        {
            return Err(Error::Protocol);
        }
        let verified = self.pool.materialize(rows, &self.roots, self.deadline)?;
        self.verified.extend(verified);
        self.bytes += bytes;
        self.batch_id = batch_id;
        Ok(())
    }
    fn finish(&self) -> Result<()> {
        remaining(self.deadline)?;
        if self.verified.len() != self.expected_files as usize || self.bytes != self.expected_bytes
        {
            return Err(Error::Protocol);
        }
        guard::require()?;
        self.roots.unchanged()?;
        for pending in &self.verified {
            remaining(self.deadline)?;
            self.roots.final_file(pending)?;
        }
        self.roots.unchanged()?;
        guard::stopped()?;
        remaining(self.deadline)?;
        Ok(())
    }
}

fn request(input: &mut impl BufRead) -> Result<Request> {
    let mut bytes = Vec::new();
    input
        .take(restore_io::MAX_REQUEST_BYTES as u64 + 1)
        .read_until(b'\n', &mut bytes)
        .map_err(|_| Error::Protocol)?;
    if bytes.last() != Some(&b'\n') || bytes.len() > restore_io::MAX_REQUEST_BYTES {
        return Err(Error::Protocol);
    }
    restore_io::decode_request(&bytes).map_err(|_| Error::Protocol)
}
fn respond(output: &mut impl Write, value: &Reply) -> Result<()> {
    value.validate().map_err(|_| Error::Protocol)?;
    let mut bytes = serde_json::to_vec(value).map_err(|_| Error::Protocol)?;
    bytes.push(b'\n');
    if bytes.len() > restore_io::MAX_REPLY_BYTES {
        return Err(Error::Protocol);
    }
    output
        .write_all(&bytes)
        .and_then(|_| output.flush())
        .map_err(|_| Error::Protocol)
}

fn run(input: &mut impl BufRead, output: &mut impl Write) -> Result<()> {
    guard::require()?;
    let mut session = Session::open(request(input)?)?;
    respond(output, &session.reply(Stage::Opened, Error::None))?;
    loop {
        let action = request(input);
        let result = match action {
            Ok(Request::Batch {
                batch_id, files, ..
            }) => session.batch(batch_id, files),
            Ok(Request::Finish { .. }) => {
                let result = session.finish();
                match result {
                    Ok(()) => {
                        // Join both workers before the finished byte-copy reply.
                        let reply = session.reply(Stage::Finished, Error::None);
                        drop(session);
                        respond(output, &reply)?;
                        return Ok(());
                    }
                    Err(e) => Err(e),
                }
            }
            _ => Err(Error::Protocol),
        };
        match result {
            Ok(()) => respond(output, &session.reply(Stage::Verified, Error::None))?,
            Err(e) => {
                let reply = session.reply(Stage::Refused, e);
                drop(session);
                respond(output, &reply)?;
                return Err(e);
            }
        }
    }
}

fn main() -> std::process::ExitCode {
    if std::env::args_os().skip(1).collect::<Vec<_>>() != ["--stdio-v1"] {
        return std::process::ExitCode::FAILURE;
    }
    let result = run(&mut io::stdin().lock(), &mut io::stdout().lock());
    if result.is_ok() {
        std::process::ExitCode::SUCCESS
    } else {
        std::process::ExitCode::FAILURE
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::{fs, os::unix::fs::PermissionsExt};

    // Trusted tiny ordinary fixtures exercise bytes/joining, without bypassing
    // the production CLI guards or claiming Root/unit/isolation acceptance.
    #[test]
    fn joined_copy_sync_full_readback_and_final_witnesses() {
        let fixture = files::Fixture::new();
        let roots = Arc::new(fixture.roots());
        let data = b"ordinary trusted restore fixture\n".repeat(128);
        let hash = format!("{:x}", Sha256::digest(&data));
        fs::write(fixture.source().join(&hash), &data).unwrap();
        fs::set_permissions(
            fixture.source().join(&hash),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        fs::write(fixture.target().join("existing"), &data).unwrap();
        fs::set_permissions(
            fixture.target().join("existing"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        let owner = fs::metadata(fixture.source()).unwrap();
        use std::os::unix::fs::MetadataExt;
        let rows = ["existing", "one", "two", "three"]
            .into_iter()
            .map(|name| restore_io::RestoreFile {
                path: format!("artifacts/{name}"),
                uid: owner.uid(),
                gid: owner.gid(),
                mode: 0o644,
                bytes: data.len() as u64,
                sha256: hash.clone(),
            })
            .collect();
        let deadline = now().unwrap() + 10_000_000_000;
        let pool = Pool::new(Arc::clone(&roots)).unwrap_or_else(|_| panic!("worker pool"));
        let verified = pool
            .materialize(rows, &roots, deadline)
            .unwrap_or_else(|_| panic!("verified copy"));
        assert_eq!(verified.len(), 4);
        for row in &verified {
            roots
                .final_file(row)
                .unwrap_or_else(|_| panic!("final witness"));
            let name = row.row.path.strip_prefix("artifacts/").unwrap();
            assert_eq!(fs::read(fixture.target().join(name)).unwrap(), data);
        }
        drop(pool);
        roots.unchanged().unwrap_or_else(|_| panic!("root anchors"));
    }

    #[test]
    fn request_line_requires_finite_complete_frame() {
        assert!(matches!(
            request(&mut &b"{\"schema_version\":1,\"kind\":\"restore.io.finish\"}\n"[..]),
            Ok(Request::Finish { .. })
        ));
        assert!(
            request(&mut &b"{\"schema_version\":1,\"kind\":\"restore.io.finish\"}"[..]).is_err()
        );
        assert!(request(&mut &b""[..]).is_err());
        assert!(request(&mut &vec![b' '; restore_io::MAX_REQUEST_BYTES + 1][..]).is_err());
    }
}
