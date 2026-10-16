//! Fixed real owner-death/restart cases in the independent capped VM.
use super::{Fixture, describe};
use crate::{
    artifacts::ArtifactRoot,
    launcher::{
        capture::CaptureLease,
        reap::OwnedChildren,
        run::{self, MonitorMode},
        seccomp::FilteredTarget,
    },
    runtime::{RuntimeConfig, ValidatedRuntimeConfig},
    spec::{ExecutionSpec, SpecPolicy, ValidatedSpec},
};
use rustix::fs::{Mode, OFlags};
use std::{
    fs,
    io::{self, Read},
    os::unix::fs::PermissionsExt,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
pub(crate) fn ready(
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    stage: &str,
) -> io::Result<Child> {
    super::verify_guest(true)?;
    let mut child = Command::new(std::env::current_exe()?)
        .args(["vm-case", "restart-residual"])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    // This PID belongs to our unreaped direct Child, not a request/frame.
    let setup = (|| {
        fs::write(
            format!(
                "{}/kf-{}/cgroup.procs",
                config.config().delegated_cgroup_root,
                spec.spec().run_id
            ),
            child.id().to_string(),
        )?;
        let value = serde_json::json!({"stage":stage,"owner_pid":std::process::id(),"residual_pid":child.id(),"run_id":spec.spec().run_id});
        fs::write("/kf/restart-ready.next", serde_json::to_vec(&value)?)?;
        fs::rename("/kf/restart-ready.next", "/kf/restart-ready")?;
        Ok::<_, io::Error>(())
    })();
    if let Err(e) = setup {
        let _ = child.kill();
        let _ = child.wait();
        return Err(e);
    }
    Ok(child)
}
fn config() -> io::Result<ValidatedRuntimeConfig> {
    let fd = rustix::fs::open(
        "/kf/restart-runtime.json",
        OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )?;
    let st = rustix::fs::fstat(&fd)?;
    if st.st_uid != 0
        || st.st_gid != 0
        || st.st_mode & 0o7777 != 0o600
        || st.st_nlink != 1
        || rustix::fs::FileType::from_raw_mode(st.st_mode) != rustix::fs::FileType::RegularFile
    {
        return Err(io::Error::other("trusted restart fixture config ownership"));
    }
    let mut bytes = Vec::new();
    std::fs::File::from(fd)
        .take(crate::runtime::MAX_CONFIG_BYTES + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > crate::runtime::MAX_CONFIG_BYTES {
        return Err(io::Error::other("restart config bound"));
    }
    let c: RuntimeConfig = serde_json::from_slice(&bytes)?;
    if !c
        .delegated_cgroup_root
        .starts_with("/sys/fs/cgroup/kf-acceptance-")
    {
        return Err(io::Error::other("fixed acceptance cgroup only"));
    }
    c.validate().map_err(io::Error::other)
}
pub(super) fn run_case(case: &str) -> io::Result<serde_json::Value> {
    if case == "restart-recover" {
        return serde_json::to_value(crate::cleanup::recover(&config()?)?)
            .map_err(io::Error::other);
    }
    if case == "restart-residual" {
        // Real partial-preparation residual: no parent-death guarantee. It is
        // bounded by the owning task and the independently capped VM, and its
        // own 30s deadline prevents an unbounded acceptance helper.
        std::thread::sleep(Duration::from_secs(30));
        return Ok(serde_json::json!({"residual_deadline":true}));
    }
    if !matches!(
        case,
        "restart-prepared" | "restart-live" | "restart-capture"
    ) {
        return Err(io::Error::other("unknown fixed restart case"));
    }
    let fixture = Fixture::new("/kf/bin/isolation-probe")?;
    let config = fixture.config()?;
    let root: ArtifactRoot = fixture.root()?;
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("../../tests/fixtures/spec.valid.json"))?;
    request.run_id = case.into();
    request.argv.push("peer".into());
    request.stdin_artifact = None;
    request.limits.cpu_time_us = 2_000_000;
    request.limits.wall_time_us = 30_000_000;
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(io::Error::other)?;
    if case == "restart-live" {
        let completed = run::execute(
            &config,
            &spec,
            &std::env::current_exe()?,
            &root,
            MonitorMode::RestartReady,
        )
        .map_err(|e| io::Error::other(format!("restart live {:?}", e.failure)))?;
        return describe(case, &spec, &completed, false);
    }
    let mut owned = OwnedChildren::begin()?;
    crate::launcher::capture::private_supervisor()?;
    let mut target = FilteredTarget::prepare(&config, &spec, &std::env::current_exe()?, &root)
        .map_err(|e| io::Error::other(format!("restart prepared {:?}", e.failure)))?;
    let (mut lease, dir) = CaptureLease::prepare(&config, &spec)?;
    let (pump, stdio) = crate::io::PipePump::prepare(target.take_stdin(), &spec, dir)?;
    target.root_mut().target.prepare_execution(&spec, stdio)?;
    let capture_case = case == "restart-capture";
    let _child = ready(
        &config,
        &spec,
        if capture_case { "capture" } else { "prepared" },
    )?;
    let deadline = Instant::now() + Duration::from_secs(15);
    while Instant::now() < deadline {
        if capture_case && std::path::Path::new("/kf/restart-cleanup").exists() {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    drop(pump);
    let capture = lease.cleanup();
    if capture_case {
        // The controller owns this exact fixture and repairs its foreign bare
        // capture entry only after proving the failed UID remains reserved.
        // Keep the delegated anchor alive for the fresh restart helper.
        std::mem::forget(fixture);
        let result = target.refuse_at(crate::outcome::ExecutionStage::Exec, None);
        let reaped = owned.finish()?;
        let e = capture
            .err()
            .ok_or_else(|| io::Error::other("expected real capture deletion failure"))?;
        return Ok(serde_json::json!({
            "case":case,"failure":result.failure,"cleanup_error":result.cleanup_failure,
            "capture_cleanup_errno":e.raw_os_error(),"reaped_owned_orphans":reaped,
            "user_exec_released":false,"owned_children_finished":true
        }));
    }
    capture?;
    let result = target.refuse_at(crate::outcome::ExecutionStage::Exec, None);
    owned.finish()?;
    fs::remove_file("/kf/restart-ready")?;
    fs::set_permissions(&fixture.publications, fs::Permissions::from_mode(0o700))?;
    Ok(serde_json::json!({"unexpected_probe_deadline":true,"cleanup_error":result.cleanup_failure}))
}
