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

// Preserve contiguous hash/path order while minimizing the heavier byte group.
// Equal-byte choices prefer the original balanced file count (including zeroes).
fn balanced_split(rows: &[restore_io::RestoreFile]) -> usize {
    if rows.len() <= 1 {
        return rows.len();
    }
    let middle = rows.len().div_ceil(2);
    let total: u64 = rows.iter().map(|row| row.bytes).sum();
    let mut prefix = 0;
    let mut best = (u64::MAX, usize::MAX);
    let mut chosen = middle;
    for (index, row) in rows.iter().take(rows.len() - 1).enumerate() {
        prefix += row.bytes;
        let split = index + 1;
        let cost = (prefix.max(total - prefix), split.abs_diff(middle));
        if cost < best {
            best = cost;
            chosen = split;
        }
    }
    chosen
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
        let count = rows.len();
        let split = balanced_split(&rows);
        let mut rows = rows;
        let second = rows.split_off(split);
        let groups = [rows, second];
        for (worker, group) in self.workers.iter().zip(groups) {
            worker
                .sender
                .send(Job::Copy(group, deadline))
                .map_err(|_| Error::Filesystem)?;
        }
        let mut pending = Vec::with_capacity(count);
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
        let second = pending.split_off(split);
        let groups = [pending, second];
        for (worker, group) in self.workers.iter().zip(groups) {
            worker
                .sender
                .send(Job::Read(group, deadline))
                .map_err(|_| Error::Filesystem)?;
        }
        let mut verified = Vec::with_capacity(count);
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

    fn weighted_rows(weights: &[u64]) -> Vec<restore_io::RestoreFile> {
        weights
            .iter()
            .enumerate()
            .map(|(index, &bytes)| restore_io::RestoreFile {
                path: format!("artifacts/file-{index}"),
                uid: 1000,
                gid: 1000,
                mode: 0o644,
                bytes,
                sha256: "0".repeat(64),
            })
            .collect()
    }

    #[test]
    fn every_small_weight_inventory_has_minimal_contiguous_heavier_group() {
        assert_eq!(balanced_split(&[]), 0);
        assert_eq!(balanced_split(&weighted_rows(&[9])), 1);
        assert_eq!(balanced_split(&weighted_rows(&[0; 5])), 3);
        assert_eq!(balanced_split(&weighted_rows(&[1, 1, 1, 20])), 3);
        assert_eq!(balanced_split(&weighted_rows(&[20, 1, 1, 1])), 1);
        for count in 2u32..=6 {
            for mut encoded in 0..3usize.pow(count) {
                let weights: Vec<_> = (0..count)
                    .map(|_| {
                        let weight = [0, 1, 13][encoded % 3];
                        encoded /= 3;
                        weight
                    })
                    .collect();
                let rows = weighted_rows(&weights);
                let split = balanced_split(&rows);
                assert!(split > 0 && split < rows.len());
                let cost = |at| {
                    let (a, b) = weights.split_at(at);
                    a.iter().sum::<u64>().max(b.iter().sum())
                };
                assert!((1..rows.len()).all(|other| cost(split) <= cost(other)));
            }
        }
    }

    #[test]
    fn skewed_groups_keep_every_copy_sync_readback_and_final_witness() {
        for weights in [[64, 64, 64, 4096], [4096, 64, 64, 64]] {
            let fixture = files::Fixture::new();
            let roots = Arc::new(fixture.roots());
            let mut rows = weighted_rows(&weights);
            use std::os::unix::fs::MetadataExt;
            let owner = fs::metadata(fixture.source()).unwrap();
            for row in &mut rows {
                let data = vec![b'x'; row.bytes as usize];
                row.sha256 = format!("{:x}", Sha256::digest(&data));
                row.uid = owner.uid();
                row.gid = owner.gid();
                let source = fixture.source().join(&row.sha256);
                fs::write(&source, &data).unwrap();
                fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
            }
            let pool = Pool::new(Arc::clone(&roots)).unwrap_or_else(|_| panic!("pool"));
            let verified = pool
                .materialize(rows, &roots, now().unwrap() + 10_000_000_000)
                .unwrap_or_else(|_| panic!("skewed verified copies"));
            assert_eq!(verified.len(), weights.len());
            for row in &verified {
                roots
                    .final_file(row)
                    .unwrap_or_else(|_| panic!("final witness"));
                let name = row.row.path.strip_prefix("artifacts/").unwrap();
                assert_eq!(
                    fs::read(fixture.target().join(name)).unwrap(),
                    vec![b'x'; row.row.bytes as usize]
                );
            }
            drop(pool);
            roots.unchanged().unwrap_or_else(|_| panic!("root anchors"));
        }
    }

    #[test]
    fn streamed_verified_pages_preserve_repeated_large_bytes_and_all_witnesses() {
        let fixture = files::Fixture::new();
        let roots = Arc::new(fixture.roots());
        let data = vec![b'v'; 8 * 1024 * 1024];
        let hash = format!("{:x}", Sha256::digest(&data));
        let source = fixture.source().join(&hash);
        fs::write(&source, &data).unwrap();
        fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
        use std::os::unix::fs::MetadataExt;
        let owner = fs::metadata(&source).unwrap();
        let rows = (0..3)
            .map(|index| restore_io::RestoreFile {
                path: format!("artifacts/stream-{index}"),
                uid: owner.uid(),
                gid: owner.gid(),
                mode: 0o644,
                bytes: data.len() as u64,
                sha256: hash.clone(),
            })
            .collect();
        let pool = Pool::new(Arc::clone(&roots)).unwrap_or_else(|_| panic!("pool"));
        let verified = pool
            .materialize(rows, &roots, now().unwrap() + 10_000_000_000)
            .unwrap_or_else(|_| panic!("streamed verified copies"));
        assert_eq!(verified.len(), 3);
        for row in &verified {
            roots
                .final_file(row)
                .unwrap_or_else(|_| panic!("final witness"));
            let name = row.row.path.strip_prefix("artifacts/").unwrap();
            assert_eq!(fs::read(fixture.target().join(name)).unwrap(), data);
            roots
                .final_file(row)
                .unwrap_or_else(|_| panic!("unchanged witness"));
        }
        drop(pool);
        roots.unchanged().unwrap_or_else(|_| panic!("root anchors"));
    }

    #[test]
    fn exact_lengths_and_unchanged_create_mode_keep_complete_readback() {
        let fixture = files::Fixture::new();
        let roots = Arc::new(fixture.roots());
        use std::os::unix::fs::MetadataExt;
        let owner = fs::metadata(fixture.source()).unwrap();
        let mut rows = Vec::new();
        for (index, size) in [0, 13, 1024 * 1024, 1024 * 1024 + 13]
            .into_iter()
            .enumerate()
        {
            let mut data = vec![b'a'; size];
            if let Some(last) = data.last_mut() {
                *last = b'z';
            }
            let hash = format!("{:x}", Sha256::digest(&data));
            fs::write(fixture.source().join(&hash), &data).unwrap();
            fs::set_permissions(
                fixture.source().join(&hash),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
            rows.push(restore_io::RestoreFile {
                path: format!("artifacts/length-{index}"),
                uid: owner.uid(),
                gid: owner.gid(),
                mode: 0o600,
                bytes: size as u64,
                sha256: hash,
            });
        }
        let pool = Pool::new(Arc::clone(&roots)).unwrap_or_else(|_| panic!("pool"));
        for _ in 0..2 {
            let verified = pool
                .materialize(rows.clone(), &roots, now().unwrap() + 10_000_000_000)
                .unwrap_or_else(|_| panic!("complete lengths including existing files"));
            assert_eq!(verified.len(), 4);
            for pending in &verified {
                roots
                    .final_file(pending)
                    .unwrap_or_else(|_| panic!("final witness"));
                let name = pending.row.path.strip_prefix("artifacts/").unwrap();
                let data = fs::read(fixture.target().join(name)).unwrap();
                assert_eq!(data.len() as u64, pending.row.bytes);
                assert_eq!(format!("{:x}", Sha256::digest(&data)), pending.row.sha256);
                assert_eq!(
                    fs::metadata(fixture.target().join(name))
                        .unwrap()
                        .permissions()
                        .mode()
                        & 0o7777,
                    0o600
                );
            }
        }
        drop(pool);
        roots.unchanged().unwrap_or_else(|_| panic!("root anchors"));
    }

    #[test]
    fn short_final_chunk_still_refuses_a_wrong_last_byte() {
        for size in [13, 1024 * 1024 + 13] {
            let fixture = files::Fixture::new();
            let roots = Arc::new(fixture.roots());
            let mut data = vec![b'a'; size];
            let expected = format!("{:x}", Sha256::digest(&data));
            *data.last_mut().unwrap() = b'z';
            let source = fixture.source().join(&expected);
            fs::write(&source, data).unwrap();
            fs::set_permissions(&source, fs::Permissions::from_mode(0o600)).unwrap();
            use std::os::unix::fs::MetadataExt;
            let owner = fs::metadata(&source).unwrap();
            let pool = Pool::new(Arc::clone(&roots)).unwrap_or_else(|_| panic!("pool"));
            assert!(matches!(
                pool.materialize(
                    vec![restore_io::RestoreFile {
                        path: "artifacts/wrong-last-byte".into(),
                        uid: owner.uid(),
                        gid: owner.gid(),
                        mode: 0o600,
                        bytes: size as u64,
                        sha256: expected,
                    }],
                    &roots,
                    now().unwrap() + 10_000_000_000
                ),
                Err(Error::Bytes)
            ));
        }
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
