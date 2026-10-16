//! Closed interpreter/JDK commands. Operator image selection remains separate;
//! validation alone neither acquires authority nor proves OS isolation.
use crate::spec::{ExecutionProfile as P, ExecutionSpec, ResourceLimits};
use kf_contracts::{judging::RunLimits, judging_v2 as v2};

fn contract_limits(r: &ResourceLimits) -> RunLimits {
    RunLimits {
        cpu_time_us: r.cpu_time_us,
        wall_time_us: r.wall_time_us,
        memory_bytes: r.memory_bytes,
        stack_bytes: r.stack_bytes,
        processes: r.processes,
        open_files: r.open_files,
        stdout_bytes: r.stdout_bytes,
        stderr_bytes: r.stderr_bytes,
        file_bytes: r.file_bytes,
        workdir_bytes: r.workdir_bytes,
    }
}

/// Explicit heap, native overhead and fixed CPU/thread ergonomics. The kernel
/// still bounds the complete JVM; printed OOM text is never kernel evidence.
fn jvm_options(limits: &ResourceLimits) -> Result<Vec<String>, &'static str> {
    let heap = v2::jvm_heap_bytes(&contract_limits(limits))? / (1024 * 1024);
    Ok(vec![
        "-XX:+UseSerialGC".into(),
        "-XX:ActiveProcessorCount=1".into(),
        "-XX:-UseContainerSupport".into(),
        "-XX:CICompilerCount=2".into(),
        "-XX:-UsePerfData".into(),
        "-XX:+DisableAttachMechanism".into(),
        "-XX:MaxMetaspaceSize=64m".into(),
        "-XX:CompressedClassSpaceSize=32m".into(),
        "-XX:ReservedCodeCacheSize=32m".into(),
        "-Xss256k".into(),
        "-Xms16m".into(),
        format!("-Xmx{heap}m"),
        "-Djava.io.tmpdir=/tmp".into(),
        "-Dfile.encoding=UTF-8".into(),
        // JDK 21's standard streams have their own encoding properties. A
        // cleared POSIX environment otherwise selects ASCII for piped output.
        "-Dstdout.encoding=UTF-8".into(),
        "-Dstderr.encoding=UTF-8".into(),
        "-Duser.language=en".into(),
        "-Duser.country=US".into(),
        "-Duser.timezone=UTC".into(),
    ])
}

pub fn argv(profile: P, limits: &ResourceLimits) -> Result<Vec<String>, &'static str> {
    let (language, preparing) = match profile {
        P::PythonCheck => (v2::Language::Python313, true),
        P::PythonRun => (v2::Language::Python313, false),
        P::JavaCompile => (v2::Language::Java21, true),
        P::JavaRun => (v2::Language::Java21, false),
        _ => return Err("profile has no managed command"),
    };
    v2::validate_startup_budget(language, &contract_limits(limits), preparing)?;
    match profile {
        P::PythonCheck | P::PythonRun => Ok([
            "/usr/bin/python3.13",
            "-I",
            "-S",
            "-B",
            if profile == P::PythonCheck {
                "/usr/lib/kf/python_check.py"
            } else {
                "/work/main.py"
            },
        ]
        .map(String::from)
        .to_vec()),
        P::JavaCompile => {
            let mut args = vec!["/usr/lib/jvm/bin/javac".into()];
            args.extend(jvm_options(limits)?.into_iter().map(|a| format!("-J{a}")));
            args.extend(
                [
                    "-proc:none",
                    "-implicit:none",
                    "-encoding",
                    "UTF-8",
                    "--release",
                    "21",
                    "-classpath",
                    "/work",
                    "-sourcepath",
                    "/work",
                    "-d",
                    "/work",
                    "/work/Main.java",
                ]
                .map(String::from),
            );
            Ok(args)
        }
        P::JavaRun => {
            let mut args = vec!["/usr/lib/jvm/bin/java".into()];
            args.extend(jvm_options(limits)?);
            args.extend(["-cp", "/work", "Main"].map(String::from));
            Ok(args)
        }
        _ => unreachable!(),
    }
}

pub(crate) fn validate(spec: &ExecutionSpec) -> Result<(), &'static str> {
    if spec.argv != argv(spec.profile, &spec.limits)?
        || !spec.env.is_empty()
        || spec.cwd != "."
        || !spec.outputs.is_empty()
    {
        return Err("managed profile differs from trusted command");
    }
    if spec.profile == P::JavaRun {
        if spec.inputs.is_empty()
            || spec.inputs.len() > v2::MAX_CLASS_FILES
            || !spec.inputs.iter().any(|f| f.path == "Main.class")
            || spec.inputs.iter().any(|f| !v2::class_name(&f.path))
            || spec.inputs.windows(2).any(|f| f[0].path >= f[1].path)
        {
            return Err("invalid Java class input set");
        }
    } else {
        let expected = if spec.profile == P::JavaCompile {
            "Main.java"
        } else {
            "main.py"
        };
        if spec.inputs.len() != 1 || spec.inputs[0].path != expected {
            return Err("managed profile requires fixed source");
        }
    }
    if spec.profile.prepares_program() && spec.stdin_artifact.is_some() {
        return Err("preparation does not consume test input");
    }
    Ok(())
}
