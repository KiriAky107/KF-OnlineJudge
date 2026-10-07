//! Fixed independent-VM acceptance entry; never present in default builds.
//! No caller-selected command, filesystem, artifact directory or configuration.
use crate::{
    artifacts::ArtifactRoot,
    launcher::run::{self, MonitorMode},
    runtime::RuntimeConfig,
    spec::{ExecutionSpec, SpecPolicy},
};
use std::{fs, io, io::Read, os::unix::fs::PermissionsExt, path::Path};
pub(crate) fn verify_guest(require_env: bool) -> io::Result<()> {
    if (require_env && std::env::var("KF_SANDBOX_INDEPENDENT_VM").as_deref() != Ok("1"))
        || fs::read_to_string("/kf/INDEPENDENT_VM")?.trim() != "isolated-test-guest"
        || !fs::read_to_string("/sys/class/dmi/id/sys_vendor")?.contains("QEMU")
        || ["/mnt/hgfs", "/home/kronecker", "/root/.ssh"]
            .iter()
            .any(|p| Path::new(p).exists())
    {
        return Err(io::Error::other("independent VM required"));
    }
    let mounts = fs::read_to_string("/proc/self/mountinfo")?;
    if ["hgfs", "9p", "virtiofs"]
        .iter()
        .any(|s| mounts.contains(s))
        || fs::read_dir("/sys/class/net")?
            .map(|e| e.map(|e| e.file_name()))
            .collect::<Result<Vec<_>, _>>()?
            != [std::ffi::OsString::from("lo")]
    {
        return Err(io::Error::other("guest shares or network"));
    }
    Ok(())
}
struct Fixture {
    cg: String,
    publications: String,
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir(&self.cg);
        let _ = fs::remove_dir_all(&self.publications);
    }
}
fn verify_snapshot(file: &crate::artifacts::SnapshotFile, pattern: bool) -> io::Result<()> {
    let seals = rustix::fs::fcntl_get_seals(file)?;
    if !seals.contains(
        rustix::fs::SealFlags::WRITE
            | rustix::fs::SealFlags::SHRINK
            | rustix::fs::SealFlags::GROW
            | rustix::fs::SealFlags::SEAL
            | rustix::fs::SealFlags::EXEC,
    ) || rustix::fs::fcntl_getfl(file)?.intersection(rustix::fs::OFlags::ACCMODE)
        != rustix::fs::OFlags::RDONLY
    {
        return Err(io::Error::other("immutable snapshot readback"));
    }
    if pattern {
        let mut reader = file.reader()?;
        let mut offset = 0;
        let mut buf = [0_u8; 4096];
        loop {
            let n = reader.read(&mut buf)?;
            if n == 0 {
                break;
            }
            for (i, b) in buf[..n].iter().enumerate() {
                if *b != (offset + i) as u8 {
                    return Err(io::Error::other("binary snapshot contents"));
                }
            }
            offset += n;
        }
        if offset as u64 != file.bytes() {
            return Err(io::Error::other("binary snapshot bytes"));
        }
    }
    Ok(())
}
fn prefix(file: &crate::artifacts::SnapshotFile) -> io::Result<String> {
    let mut b = Vec::new();
    file.reader()?.take(64).read_to_end(&mut b)?;
    Ok(b.iter().map(|b| format!("{b:02x}")).collect())
}
pub fn run_case(case: &str) -> io::Result<serde_json::Value> {
    verify_guest(true)?;
    let rollback = case.strip_prefix("cancel-").filter(|s| {
        matches!(
            *s,
            "blocked"
                | "namespace"
                | "root"
                | "proc"
                | "inputs"
                | "identity"
                | "seccomp"
                | "io"
                | "exec"
        )
    });
    let failing = case.strip_prefix("fail-").filter(|s| {
        matches!(
            *s,
            "namespace" | "root" | "proc" | "inputs" | "rlimits" | "identity" | "seccomp" | "io"
        )
    });
    let mode = match case {
        "binary" | "stdout" | "stderr" | "wall" | "leftover" | "early" | "disk" | "export" => {
            MonitorMode::Normal
        }
        "cancel" => MonitorMode::CancelAfter(100000),
        "monitor" => MonitorMode::FailAfter(100000),
        _ if rollback.is_some() || failing.is_some() => MonitorMode::Normal,
        _ => return Err(io::Error::other("unknown fixed case")),
    };
    let id = format!("execution-{case}");
    let cg = format!("/sys/fs/cgroup/kf-acceptance-{}", std::process::id());
    let publications = format!("/kf/execution-publications-{}", std::process::id());
    fs::create_dir(&cg)?;
    fs::create_dir(&publications)?;
    fs::set_permissions(&cg, fs::Permissions::from_mode(0o700))?;
    fs::set_permissions(&publications, fs::Permissions::from_mode(0o700))?;
    let fixture = Fixture { cg, publications };
    fs::copy(
        "/kf/bin/execution-fixture",
        format!("{}/program-001", fixture.publications),
    )?;
    fs::write(
        format!("{}/input-001", fixture.publications),
        vec![0xa5; 65537],
    )?;
    let root = ArtifactRoot::from_directory(rustix::fs::open(
        &fixture.publications,
        rustix::fs::OFlags::PATH | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )?)?;
    let mut config: RuntimeConfig = serde_json::from_str(include_str!(
        "../../../../deploy/sandbox.runtime.example.json"
    ))?;
    config.delegated_cgroup_root = fixture.cg.clone();
    let config = config.validate().map_err(io::Error::other)?;
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("../tests/fixtures/spec.valid.json"))?;
    request.run_id = id;
    request.argv.push(case.into());
    request.limits.cpu_time_us = 2_000_000;
    request.limits.wall_time_us = 8_000_000;
    if matches!(case, "stdout" | "stderr") {
        request.limits.stdout_bytes = 4096;
        request.limits.stderr_bytes = 4096;
    }
    if matches!(case, "wall") {
        request.limits.wall_time_us = 200000;
    }
    if case == "binary" {
        request.outputs = vec!["result.bin".into()];
    }
    if case == "disk" {
        request.limits.file_bytes = 131072;
        request.limits.workdir_bytes = 262144;
        request.limits.stdout_bytes = 4096;
        request.limits.stderr_bytes = 4096;
    }
    if case == "export" {
        request.outputs = vec!["valid.bin".into(), "escape".into()];
    }
    if case == "fail-io" {
        request.limits.file_bytes = 65536;
        request.limits.stdout_bytes = 131072;
        request.limits.stderr_bytes = 131072;
        request.stdin_artifact = None;
    }
    if case == "fail-inputs" {
        fs::remove_file(format!("{}/program-001", fixture.publications))?;
    }
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(io::Error::other)?;
    if let Some(stage) = rollback {
        return crate::launcher::rollback::cancel_stage(
            stage,
            &config,
            &spec,
            &std::env::current_exe()?,
            &root,
        );
    }
    if let Some(stage) = failing {
        return crate::launcher::rollback::fail_stage(
            stage,
            &config,
            &spec,
            &std::env::current_exe()?,
            &root,
        );
    }
    let completed =
        run::execute(&config, &spec, &std::env::current_exe()?, &root, mode).map_err(|e| {
            io::Error::other(format!(
                "initialization {:?} {:?} cleanup {:?}",
                e.failure.stage, e.failure.errno, e.cleanup_failure
            ))
        })?;
    verify_snapshot(&completed.stdout, case == "binary")?;
    verify_snapshot(&completed.stderr, case == "binary")?;
    for o in &completed.outputs {
        verify_snapshot(&o.file, case == "binary")?;
    }
    let outcome = completed.report.classify(&spec).map_err(io::Error::other)?;
    let outputs=completed.outputs.iter().map(|o|Ok(serde_json::json!({"path":o.path,"bytes":o.file.bytes(),"prefix_hex":prefix(&o.file)?}))).collect::<io::Result<Vec<_>>>()?;
    let result = serde_json::json!({"case":case,"immutable_snapshots_verified":true,"complete_binary_content_verified":case=="binary","report":completed.report,"outcome":outcome,"proof":completed.proof,"stdout_prefix_hex":prefix(&completed.stdout)?,"stderr_prefix_hex":prefix(&completed.stderr)?,"outputs":outputs});
    Ok(result)
}
