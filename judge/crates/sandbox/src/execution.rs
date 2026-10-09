//! Ordinary operator entry: authenticated configuration, finite helper scope,
//! and the same per-task kernel gate exercised by independent-VM acceptance.
use crate::{
    artifacts::ArtifactRoot,
    launcher::{blocked::InitializationFailure, run},
    outcome::{ErrorCode, ExecutionError, ExecutionStage},
    runtime::{RuntimeConfig, ValidatedRuntimeConfig},
    spec::{ExecutionSpec, ValidatedSpec},
};
use rustix::{
    fd::{AsRawFd, OwnedFd},
    fs::{Mode, OFlags},
};
use serde::Serialize;
use std::{
    fs::File,
    io::{self, Read, Write},
    path::Path,
};

#[derive(Debug, Serialize)]
pub struct RunFailure {
    pub error: &'static str,
    pub failure: Option<ExecutionError>,
    pub cleanup_failure: Option<ExecutionError>,
}
fn refused(code: &'static str, e: &io::Error) -> RunFailure {
    RunFailure {
        error: code,
        failure: Some(crate::launcher::blocked::failure(
            if code == "output_publication_failed" {
                ExecutionStage::ArtifactExport
            } else {
                ExecutionStage::Preparation
            },
            if code == "output_publication_failed" {
                ErrorCode::ArtifactExportFailed
            } else {
                ErrorCode::CapabilityUnavailable
            },
            Some(e),
        )),
        cleanup_failure: None,
    }
}
fn invalid(code: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, code)
}
impl From<InitializationFailure> for RunFailure {
    fn from(e: InitializationFailure) -> Self {
        Self {
            error: "sandbox_initialization_failed",
            failure: Some(e.failure),
            cleanup_failure: e.cleanup_failure,
        }
    }
}
fn read_pinned(fd: OwnedFd, limit: u64) -> io::Result<Vec<u8>> {
    let st = rustix::fs::fstat(&fd)?;
    if rustix::fs::FileType::from_raw_mode(st.st_mode) != rustix::fs::FileType::RegularFile
        || st.st_nlink != 1
        || st.st_size < 0
        || st.st_size as u64 > limit
    {
        return Err(invalid("invalid bounded regular file"));
    }
    let read = rustix::fs::open(
        format!("/proc/self/fd/{}", fd.as_raw_fd()),
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NONBLOCK | OFlags::NOCTTY,
        Mode::empty(),
    )?;
    let mut bytes = Vec::new();
    File::from(read).take(limit + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > limit {
        return Err(invalid("file exceeds bound"));
    }
    Ok(bytes)
}
pub fn read_bounded_file(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    if limit == 0 || limit > 262_144 {
        return Err(invalid("bounded JSON ceiling"));
    }
    let fd = rustix::fs::open(
        path,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    read_pinned(fd, limit)
}
pub fn read_spec(path: &Path) -> io::Result<ExecutionSpec> {
    serde_json::from_slice(&read_bounded_file(path, crate::spec::MAX_SPEC_BYTES)?)
        .map_err(|_| invalid("invalid execution JSON"))
}
pub fn load_config(path: &Path) -> io::Result<ValidatedRuntimeConfig> {
    let parent = crate::isolation::root::open_directory(
        path.parent().ok_or_else(|| invalid("config parent"))?,
    )?;
    let name = path.file_name().ok_or_else(|| invalid("config name"))?;
    let fd = rustix::fs::openat(
        &parent,
        name,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let st = rustix::fs::fstat(&fd)?;
    if st.st_uid != 0 || st.st_gid != 0 || st.st_mode & 0o7022 != 0 {
        return Err(invalid("untrusted configuration owner/mode"));
    }
    let config: RuntimeConfig =
        serde_json::from_slice(&read_pinned(fd, crate::runtime::MAX_CONFIG_BYTES)?)
            .map_err(|_| invalid("invalid runtime JSON"))?;
    config
        .validate()
        .map_err(|_| invalid("invalid runtime policy"))
}
/// These checks do not certify namespace visibility. Each task must still
/// pass every actual namespace/root/identity/seccomp/exec acknowledgement.
pub(crate) fn authorize(config: &ValidatedRuntimeConfig) -> io::Result<()> {
    crate::launcher::entry::ensure_single_threaded()?;
    if !rustix::process::getuid().is_root()
        || !rustix::process::geteuid().is_root()
        || !rustix::process::getgid().is_root()
        || !rustix::process::getegid().is_root()
    {
        return Err(invalid(
            "dedicated root helper required; setuid invocation refused",
        ));
    }
    let o = crate::probe::observe();
    if o.os != "linux"
        || o.architecture != "x86_64"
        || o.kernel_release
            .as_deref()
            .and_then(crate::runtime::kernel_supported)
            != Some(true)
        || o.current_process_seccomp_mode != Some(0)
    {
        return Err(invalid("unsupported helper platform/kernel/filter"));
    }
    let c = config.config();
    for path in [&c.state_root, &c.tasks_root, &c.image_root] {
        crate::isolation::root::open_directory(Path::new(path))?;
    }
    // The operator owns the image. An untrusted request cannot change roots,
    // image revision, delegation, identity pool or helper reservations.
    let image = crate::isolation::root::open_directory(Path::new(&c.image_root))?;
    let manifest = rustix::fs::openat(
        &image,
        "manifest.json",
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let st = rustix::fs::fstat(&manifest)?;
    if st.st_uid != 0 || st.st_gid != 0 || st.st_mode & 0o7022 != 0 {
        return Err(invalid("image manifest ownership"));
    }
    let bytes = read_pinned(manifest, crate::image::MAX_CATALOG_BYTES)?;
    if let Some(identity) = &c.runtime_identity {
        crate::image::verify(&image, identity, &bytes)?;
    } else {
        let m: serde_json::Value =
            serde_json::from_slice(&bytes).map_err(|_| invalid("image manifest JSON"))?;
        if m != serde_json::json!({"image_kind":"linux_gnu_cxx_x86_64_v1","architecture":"x86_64","revision":1})
        {
            return Err(invalid("unsupported image manifest"));
        }
    }
    let own = std::fs::read_to_string("/proc/self/cgroup")?;
    let path = own
        .lines()
        .find_map(|s| s.strip_prefix("0::"))
        .filter(|s| s.starts_with('/') && !s.split('/').any(|p| p == ".." || p == "."))
        .ok_or_else(|| invalid("unified helper cgroup"))?;
    let full = format!("/sys/fs/cgroup{path}");
    if full == c.delegated_cgroup_root || full.starts_with(&(c.delegated_cgroup_root.clone() + "/"))
    {
        return Err(invalid("helper must be outside task delegation"));
    }
    let scope = crate::isolation::root::open_directory(Path::new(&full))?;
    if rustix::fs::fstatfs(&scope)?.f_type as u64 != 0x6367_7270 {
        return Err(invalid("helper cgroup v2"));
    }
    let read = |name| crate::cgroup::read(&scope, name);
    let number = |name| {
        read(name)?
            .trim()
            .parse::<u64>()
            .map_err(|_| invalid("finite helper cgroup required"))
    };
    let b = &c.aggregate;
    if number("memory.max")? > b.supervisor_memory_bytes
        || number("memory.swap.max")? != 0
        || number("pids.max")? > u64::from(b.supervisor_processes)
    {
        return Err(invalid("helper resource reservation missing"));
    }
    let cpu = read("cpu.max")?;
    let parts: Vec<_> = cpu.split_whitespace().collect();
    if parts.len() != 2 {
        return Err(invalid("helper CPU budget"));
    }
    let q = parts[0]
        .parse::<u64>()
        .map_err(|_| invalid("finite helper CPU quota"))?;
    let p = parts[1]
        .parse::<u64>()
        .map_err(|_| invalid("helper CPU period"))?;
    if q == 0
        || p == 0
        || u128::from(q) * u128::from(b.cpu_period_us)
            > u128::from(b.supervisor_cpu_quota_us) * u128::from(p)
    {
        return Err(invalid("helper CPU reservation exceeded"));
    }
    Ok(())
}
pub(crate) fn execute(
    config: &ValidatedRuntimeConfig,
    request: ExecutionSpec,
    artifacts: &ArtifactRoot,
    mode: run::MonitorMode<'_>,
) -> Result<(run::Completed, ValidatedSpec), RunFailure> {
    authorize(config).map_err(|e| refused("runtime_not_ready", &e))?;
    let spec = request
        .validate(&config.spec_policy())
        .map_err(|_| refused("invalid_execution_spec", &invalid("request policy")))?;
    let result = run::execute(
        config,
        &spec,
        &std::env::current_exe().map_err(|e| refused("helper_binary_unavailable", &e))?,
        artifacts,
        mode,
    )?;
    result
        .report
        .classify(&spec)
        .map_err(|_| refused("invalid_execution_report", &invalid("report")))?;
    Ok((result, spec))
}
/// Operator-only file publication. Request IDs never choose host paths.
pub fn run_files(
    config_path: &Path,
    spec_path: &Path,
    artifacts_path: &Path,
    output_path: &Path,
) -> Result<serde_json::Value, RunFailure> {
    let config = load_config(config_path).map_err(|e| refused("untrusted_runtime_config", &e))?;
    authorize(&config).map_err(|e| refused("runtime_not_ready", &e))?;
    let request = read_spec(spec_path).map_err(|e| refused("invalid_execution_spec", &e))?;
    let fd = crate::isolation::root::open_directory(artifacts_path)
        .map_err(|e| refused("invalid_artifact_publication", &e))?;
    let artifacts = ArtifactRoot::from_directory(fd)
        .map_err(|e| refused("invalid_artifact_publication", &e))?;
    let output = crate::isolation::root::open_directory(output_path)
        .map_err(|e| refused("invalid_output_publication", &e))?;
    let (completed, spec) = execute(&config, request, &artifacts, run::MonitorMode::Normal)?;
    let outcome = completed
        .report
        .classify(&spec)
        .map_err(|_| refused("invalid_execution_report", &invalid("outcome")))?;
    // Snapshot files are immutable; publish only after complete cleanup.
    if completed.report.cleanup != crate::outcome::CleanupState::Complete {
        return Err(refused(
            "cleanup_incomplete",
            &invalid("publication withheld"),
        ));
    }
    let total = completed
        .outputs
        .iter()
        .try_fold(
            completed.stdout.bytes() + completed.stderr.bytes(),
            |n, f| n.checked_add(f.file.bytes()),
        )
        .ok_or_else(|| refused("output_publication_failed", &invalid("publication budget")))?;
    if total > config.config().aggregate.supervisor_workdir_bytes {
        return Err(refused(
            "output_publication_failed",
            &invalid("publication budget"),
        ));
    }
    let name = spec.spec().run_id.as_str();
    let temporary = format!(".{name}.pending");
    rustix::fs::mkdirat(&output, temporary.as_str(), Mode::from_raw_mode(0o700))
        .map_err(|e| refused("output_publication_failed", &e.into()))?;
    let dest = rustix::fs::openat(
        &output,
        temporary.as_str(),
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(|e| refused("output_publication_failed", &e.into()))?;
    let mut installed = false;
    let publication = (|| {
        for (n, file) in [("stdout", &completed.stdout), ("stderr", &completed.stderr)] {
            publish(&dest, n, file)?;
        }
        for (i, file) in completed.outputs.iter().enumerate() {
            publish(&dest, &format!("artifact-{i}"), &file.file)?;
        }
        rustix::fs::fsync(&dest)?;
        rustix::fs::renameat_with(
            &output,
            temporary.as_str(),
            &output,
            name,
            rustix::fs::RenameFlags::NOREPLACE,
        )?;
        installed = true;
        rustix::fs::fsync(&output)?;
        Ok::<_, io::Error>(())
    })();
    if let Err(e) = publication {
        if installed {
            return Err(refused("output_publication_failed", &e));
        }
        // Only fixed names we just created, beneath our exclusive held inode.
        // Never recurse or delete a pre-existing destination publication.
        for n in ["stdout".to_owned(), "stderr".to_owned()]
            .into_iter()
            .chain((0..completed.outputs.len()).map(|i| format!("artifact-{i}")))
        {
            let _ = rustix::fs::unlinkat(&dest, n.as_str(), rustix::fs::AtFlags::empty());
        }
        let _ = rustix::fs::unlinkat(&output, temporary.as_str(), rustix::fs::AtFlags::REMOVEDIR);
        return Err(refused("output_publication_failed", &e));
    }
    Ok(
        serde_json::json!({"report":completed.report,"outcome":outcome,"publication":name,"proof":completed.proof}),
    )
}
fn publish(parent: &OwnedFd, name: &str, file: &crate::artifacts::SnapshotFile) -> io::Result<()> {
    let fd = rustix::fs::openat(
        parent,
        name,
        OFlags::WRONLY | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::from_raw_mode(0o600),
    )?;
    let mut out = File::from(fd);
    let copied = io::copy(&mut file.reader()?, &mut out)?;
    if copied != file.bytes() {
        return Err(invalid("snapshot publication bytes"));
    }
    out.flush()?;
    out.sync_all()
}
