//! Finite resource probes, executable only in the verified independent VM.
use super::{Fixture, describe};
use crate::{
    launcher::run::{self, MonitorMode},
    spec::{ExecutionSpec, SpecPolicy},
};
use std::io;

pub(super) fn run_case(case: &str) -> io::Result<serde_json::Value> {
    let mode = match case {
        "resource-cpu" => "cpu",
        "resource-oom" => "oom",
        "resource-fork" => "fork",
        "resource-threads" => "threads",
        "resource-fds" => "fds",
        _ => return Err(io::Error::other("unknown resource case")),
    };
    let fixture = Fixture::new("/kf/bin/resource-fixture")?;
    let config = fixture.config()?;
    let root = fixture.root()?;
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("../../tests/fixtures/spec.valid.json"))?;
    request.run_id = case.into();
    request.argv.push(mode.into());
    request.stdin_artifact = None;
    request.limits.cpu_time_us = if mode == "cpu" { 500000 } else { 2_000_000 };
    request.limits.wall_time_us = 8_000_000;
    if mode == "oom" {
        request.limits.memory_bytes = 32 << 20;
    }
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(io::Error::other)?;
    let completed = run::execute(
        &config,
        &spec,
        &std::env::current_exe()?,
        &root,
        MonitorMode::Normal,
    )
    .map_err(|e| io::Error::other(format!("resource initialization: {:?}", e.failure)))?;
    describe(case, &spec, &completed, false)
}
