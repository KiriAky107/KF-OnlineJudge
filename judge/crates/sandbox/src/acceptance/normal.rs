//! Fixed language fixtures using the same private root execution pipeline.
use super::{Fixture, describe};
use crate::{
    launcher::run::{self, MonitorMode},
    outcome::ExecutionOutcome,
    spec::{ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits, SpecPolicy},
};
use std::{fs, io, io::Read};

pub(super) fn run_case(case: &str) -> io::Result<serde_json::Value> {
    let (program, language, compile) = match case {
        "normal-static" => ("/kf/bin/normal-static", "C", false),
        "normal-c" | "normal-empty" | "normal-nonzero" => ("/kf/bin/normal-c", "C", false),
        "normal-cpp" => ("/kf/bin/normal-cpp", "C++", false),
        "normal-compile-c" => ("/kf/bin/normal-c", "C", true),
        "normal-compile-cpp" => ("/kf/bin/normal-cpp", "C++", true),
        _ => return Err(io::Error::other("unknown normal case")),
    };
    let fixture = Fixture::new(program)?;
    let config = fixture.config()?;
    // Software-emulated GCC consumes much more CPU than the native target.
    // Only this fixed VM fixture gets a larger, still finite compiler budget.
    let mut compiler_config = config.config().clone();
    compiler_config.per_task_ceiling.cpu_time_us = 30_000_000;
    compiler_config.per_task_ceiling.wall_time_us = 60_000_000;
    let compiler_config = compiler_config.validate().map_err(io::Error::other)?;
    let root = fixture.root()?;
    let binary = std::env::current_exe()?;
    let mut request: ExecutionSpec =
        serde_json::from_str(include_str!("../../tests/fixtures/spec.valid.json"))?;
    let mut compilation = None;
    if compile {
        let cpp = language == "C++";
        let source_name = if cpp { "main.cpp" } else { "main.c" };
        let source = if cpp {
            include_str!("../../tests/fixtures/normal.cpp")
        } else {
            include_str!("../../tests/fixtures/normal.c")
        };
        fs::write(format!("{}/source-001", fixture.publications), source)?;
        let mut compiler = request.clone();
        compiler.run_id = format!("{case}-build");
        compiler.profile = if cpp {
            ExecutionProfile::CompileCpp
        } else {
            ExecutionProfile::CompileC
        };
        compiler.limits = ResourceLimits::for_profile(compiler.profile);
        compiler.limits.cpu_time_us = 30_000_000;
        compiler.limits.wall_time_us = 60_000_000;
        compiler.argv = vec![
            if cpp { "/usr/bin/g++" } else { "/usr/bin/gcc" }.into(),
            "-O0".into(),
            "-Wall".into(),
            "-Wextra".into(),
            "-Werror".into(),
            format!("/work/{source_name}"),
            "-o".into(),
            "/work/main.bin".into(),
        ];
        compiler.inputs = vec![InputArtifact {
            artifact_id: "source-001".into(),
            path: source_name.into(),
        }];
        compiler.outputs = vec!["main.bin".into()];
        compiler.stdin_artifact = None;
        let spec = compiler
            .validate(&SpecPolicy::default())
            .map_err(io::Error::other)?;
        let completed = run::execute(&compiler_config, &spec, &binary, &root, MonitorMode::Normal)
            .map_err(|e| io::Error::other(format!("compiler initialization: {:?}", e.failure)))?;
        let result = describe(case, &spec, &completed, false)?;
        if completed.report.classify(&spec).map_err(io::Error::other)?
            != (ExecutionOutcome::Exited { code: 0 })
            || completed.outputs.len() != 1
        {
            return Err(io::Error::other(format!(
                "sandbox compilation failed: {result}"
            )));
        }
        let output = &completed.outputs[0].file;
        let mut magic = [0_u8; 4];
        output.reader()?.read_exact(&mut magic)?;
        if magic != *b"\x7fELF" || output.bytes() < 4096 {
            return Err(io::Error::other("compiler output is not ELF"));
        }
        // Publish the sealed export only after full compiler teardown. The
        // next task snapshots it anew as its single approved executable input.
        let mut destination = fs::File::create(format!("{}/program-001", fixture.publications))?;
        let copied = io::copy(&mut output.reader()?, &mut destination)?;
        if copied != output.bytes() {
            return Err(io::Error::other("compiler publication length"));
        }
        compilation = Some(result);
    }
    request.run_id = format!("{case}-run");
    let mode = match case {
        "normal-empty" => "empty",
        "normal-nonzero" => "nonzero",
        _ => "normal",
    };
    request.argv.push(mode.into());
    if mode == "empty" {
        request.stdin_artifact = None;
    }
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(io::Error::other)?;
    let completed = run::execute(&config, &spec, &binary, &root, MonitorMode::Normal)
        .map_err(|e| io::Error::other(format!("normal initialization: {:?}", e.failure)))?;
    let expected = format!(
        "{language} bytes={}\n",
        if mode == "empty" { 0 } else { 65537 }
    );
    let mut bytes = Vec::new();
    completed
        .stdout
        .reader()?
        .take(64)
        .read_to_end(&mut bytes)?;
    if bytes != expected.as_bytes() || completed.stdout.bytes() != bytes.len() as u64 {
        return Err(io::Error::other("normal complete stdout content"));
    }
    let mut result = describe(case, &spec, &completed, false)?;
    result["complete_stdout_content_verified"] = true.into();
    result["compilation"] = compilation.into();
    Ok(result)
}
