//! Fixed complete-boundary probes inside the independent, bounded VM only.
use super::{Fixture, describe};
use crate::{
    launcher::run::{self, MonitorMode},
    spec::{ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits, SpecPolicy},
};
use std::{fs, io, os::unix::fs::symlink};

pub(super) fn run_case(case: &str) -> io::Result<serde_json::Value> {
    if case == "isolation-peer"
        || case.strip_prefix("isolation-").is_some_and(|s| {
            ["danger-", "identity-", "cross-"]
                .iter()
                .any(|p| s.starts_with(p))
        })
    {
        return matrix_case(case);
    }
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

fn matrix_case(case: &str) -> io::Result<serde_json::Value> {
    let (mode, profile) = if case == "isolation-peer" {
        ("peer", ExecutionProfile::Native)
    } else {
        let (mode, profile) = case
            .strip_prefix("isolation-")
            .and_then(|s| s.split_once('-'))
            .ok_or_else(|| io::Error::other("fixed matrix case"))?;
        if !matches!(mode, "danger" | "identity" | "cross") {
            return Err(io::Error::other("fixed matrix mode"));
        }
        (
            mode,
            match profile {
                "native" => ExecutionProfile::Native,
                "c" => ExecutionProfile::CompileC,
                "cpp" => ExecutionProfile::CompileCpp,
                _ => return Err(io::Error::other("fixed matrix profile")),
            },
        )
    };
    let fixture = Fixture::new("/kf/bin/isolation-probe")?;
    let config = fixture.config()?;
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("../../tests/fixtures/spec.valid.json"))?;
    request.run_id = case.into();
    request.profile = profile;
    request.stdin_artifact = None;
    request.limits = ResourceLimits::for_profile(profile);
    request.limits.cpu_time_us = 2_000_000;
    request.limits.wall_time_us = if mode == "peer" {
        30_000_000
    } else {
        8_000_000
    };
    let label = match profile {
        ExecutionProfile::Native => "native",
        ExecutionProfile::CompileC => "compile_c",
        ExecutionProfile::CompileCpp => "compile_cpp",
    };
    if profile == ExecutionProfile::Native {
        request.argv.extend([mode.into(), label.into()]);
    } else {
        // Exercise user-controlled compiler wrappers through the real GNU
        // driver. The image probe is fixed; we never replace GCC/G++ itself.
        fs::write(
            format!("{}/source-001", fixture.publications),
            b"int isolated_source;\n",
        )?;
        request.inputs = vec![InputArtifact {
            artifact_id: "source-001".into(),
            path: "probe.c".into(),
        }];
        request.argv = vec![
            if profile == ExecutionProfile::CompileC {
                "/usr/bin/gcc"
            } else {
                "/usr/bin/g++"
            }
            .into(),
            "-fsyntax-only".into(),
            "/work/probe.c".into(),
            "-wrapper".into(),
            format!("/usr/bin/kf-isolation-probe,{mode},{label}"),
        ];
    }
    if mode == "cross" {
        let frame = fs::read("/kf/isolation-peer-frame")?;
        if frame.len() > 512 {
            return Err(io::Error::other("peer frame size"));
        }
        fs::write(format!("{}/peer-001", fixture.publications), frame)?;
        request.inputs.push(InputArtifact {
            artifact_id: "peer-001".into(),
            path: "peer".into(),
        });
    }
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(io::Error::other)?;
    let completed = run::execute(
        &config,
        &spec,
        &std::env::current_exe()?,
        &fixture.root()?,
        MonitorMode::Normal,
    )
    .map_err(|e| io::Error::other(format!("matrix initialization: {:?}", e.failure)))?;
    let mut value = describe(case, &spec, &completed, false)?;
    let mut stdout = String::new();
    use std::io::Read;
    completed
        .stdout
        .reader()?
        .take(2048)
        .read_to_string(&mut stdout)?;
    value["probe_stdout"] = stdout.into();
    Ok(value)
}
