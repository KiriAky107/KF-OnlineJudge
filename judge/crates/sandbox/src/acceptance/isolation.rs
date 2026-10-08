//! Fixed complete-boundary probes inside the independent, bounded VM only.
use super::{Fixture, describe};
use crate::{
    launcher::run::{self, MonitorMode},
    spec::{ExecutionSpec, InputArtifact, SpecPolicy},
};
use std::{fs, io, os::unix::fs::symlink};

pub(super) fn run_case(case: &str) -> io::Result<serde_json::Value> {
    if !matches!(case, "isolation-files" | "isolation-inherited-fd") {
        return Err(io::Error::other("unknown isolation case"));
    }
    let fixture = Fixture::new("/kf/bin/isolation-probe")?;
    let config = fixture.config()?;
    let image = &config.config().image_root;
    fs::write(format!("{image}/image-sentinel"), b"readonly-image")?;
    fs::write("/kf/outside-sentinel", b"outside-private-root")?;
    symlink("../../kf/outside-sentinel", format!("{image}/escape-image"))?;
    fs::write(
        format!("{}/data-001", fixture.publications),
        b"protected-input",
    )?;
    fs::write(
        format!("{}/stdin-001", fixture.publications),
        b"private-stdin",
    )?;
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("../../tests/fixtures/spec.valid.json"))?;
    request.run_id = case.into();
    request.argv.push("files".into());
    request.inputs.push(InputArtifact {
        artifact_id: "data-001".into(),
        path: "source/input".into(),
    });
    request.stdin_artifact = Some("stdin-001".into());
    request.limits.cpu_time_us = 2_000_000;
    request.limits.wall_time_us = 8_000_000;
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(io::Error::other)?;
    let root = fixture.root()?;
    // The fresh privileged exec role must refuse, rather than inherit a caller
    // directory capability even when it was accidentally left without CLOEXEC.
    let inherited = if case == "isolation-inherited-fd" {
        let fd = rustix::fs::open(
            "/kf",
            rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::DIRECTORY,
            rustix::fs::Mode::empty(),
        )?;
        rustix::io::fcntl_setfd(&fd, rustix::io::FdFlags::empty())?;
        Some(fd)
    } else {
        None
    };
    let result = run::execute(
        &config,
        &spec,
        &std::env::current_exe()?,
        &root,
        MonitorMode::Normal,
    );
    drop(inherited);
    fs::remove_file(format!("{image}/escape-image"))?;
    if fs::read("/kf/outside-sentinel")? != b"outside-private-root"
        || fs::read(format!("{image}/image-sentinel"))? != b"readonly-image"
        || fs::read(format!("{}/data-001", fixture.publications))? != b"protected-input"
    {
        return Err(io::Error::other("outside or input sentinel changed"));
    }
    if case == "isolation-inherited-fd" {
        let failure = result
            .err()
            .ok_or_else(|| io::Error::other("inherited directory not refused"))?;
        if failure.cleanup_failure.is_some() {
            return Err(io::Error::other("inherited descriptor cleanup failed"));
        }
        return Ok(
            serde_json::json!({"case":case,"refused_before_exec":true,"failure":failure.failure,"cleanup_failure":failure.cleanup_failure,"outside_sentinels_unchanged":true}),
        );
    }
    let completed =
        result.map_err(|e| io::Error::other(format!("isolation preparation: {:?}", e.failure)))?;
    let mut value = describe(case, &spec, &completed, false)?;
    value["outside_sentinels_unchanged"] = true.into();
    Ok(value)
}
