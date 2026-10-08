use clap::{Parser, Subcommand};
use kf_sandbox::spec::{ExecutionSpec, MAX_SPEC_BYTES, SpecPolicy};
use std::fs::File;
use std::io::Read;
use std::path::PathBuf;
use std::process::ExitCode;

#[derive(Parser)]
#[command(version, about = "KF Linux sandbox entrypoint")]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Observe the current Linux environment without changing it.
    Probe,
    #[cfg(all(target_os = "linux", feature = "vm-acceptance"))]
    #[command(hide = true)]
    VmCase { case: String },
    /// Validate a local execution specification without running any program.
    Validate {
        #[arg(long)]
        spec: PathBuf,
    },
    /// Check trusted runtime policy and report unavailable/unverified capabilities.
    CheckRuntime {
        #[arg(long)]
        config: PathBuf,
    },
    /// Dedicated root helper: requires trusted runtime, finite scope and all isolation gates.
    Run {
        #[arg(long)]
        config: Option<PathBuf>,
        #[arg(long)]
        spec: Option<PathBuf>,
        #[arg(long)]
        artifacts: Option<PathBuf>,
        #[arg(long)]
        output: Option<PathBuf>,
    },
    /// One authenticated Worker request, in a dedicated finite root helper scope.
    ServeOne {
        #[arg(long)]
        config: PathBuf,
        #[arg(long)]
        socket: PathBuf,
    },
    /// Root factory: finite service lifetime and fresh authenticated helpers.
    Factory {
        #[arg(long)]
        config: PathBuf,
        #[arg(long, default_value_t = 3600)]
        lifetime_seconds: u64,
    },
    #[cfg(target_os = "linux")]
    #[command(hide = true)]
    FactoryChild {
        #[arg(long)]
        config: PathBuf,
        #[arg(long)]
        run_id: String,
    },
    /// Internal trusted launcher control entry; never runs a user program.
    #[cfg(target_os = "linux")]
    #[command(hide = true)]
    Launcher {
        #[arg(long)]
        control_socket: PathBuf,
    },
    /// Internal bootstrap: stops itself and cannot execute a user command.
    #[cfg(target_os = "linux")]
    #[command(hide = true)]
    Bootstrap {
        #[arg(long)]
        parent_pid: u32,
        #[arg(long)]
        initialize_namespaces: bool,
        #[arg(long, requires = "initialize_namespaces")]
        initialize_root: bool,
    },
    /// Internal fresh PID-1 helper; refuses arbitrary commands and user exec.
    #[cfg(target_os = "linux")]
    #[command(hide = true)]
    NamespaceInit {
        #[arg(long)]
        parent_pid: u32,
        #[arg(long)]
        initialize_root: bool,
    },
}

fn main() -> ExitCode {
    match Cli::parse().command {
        Command::Probe => match serde_json::to_string_pretty(&kf_sandbox::probe::observe()) {
            Ok(report) => {
                println!("{report}");
                ExitCode::SUCCESS
            }
            Err(error) => {
                eprintln!("cannot encode environment observation: {error}");
                ExitCode::FAILURE
            }
        },
        #[cfg(all(target_os = "linux", feature = "vm-acceptance"))]
        Command::VmCase { case } => match kf_sandbox::acceptance::run_case(&case) {
            Ok(report) => {
                println!("{}", report);
                ExitCode::SUCCESS
            }
            Err(e) => {
                eprintln!("VM_CASE_FAILED: {e}");
                ExitCode::FAILURE
            }
        },
        Command::Run {
            config,
            spec,
            artifacts,
            output,
        } => {
            let (Some(config), Some(spec), Some(artifacts), Some(output)) =
                (config, spec, artifacts, output)
            else {
                return spec_error("execution_configuration_required", None);
            };
            #[cfg(target_os = "linux")]
            match kf_sandbox::execution::run_files(&config, &spec, &artifacts, &output) {
                Ok(result) => {
                    println!("{result}");
                    ExitCode::SUCCESS
                }
                Err(error) => {
                    eprintln!("{}", serde_json::json!(error));
                    ExitCode::from(2)
                }
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = (config, spec, artifacts, output);
                spec_error("unsupported_platform", None)
            }
        }
        Command::ServeOne { config, socket } => {
            #[cfg(target_os = "linux")]
            match kf_sandbox::rpc::serve_one(&config, &socket) {
                Ok(result) => {
                    println!("{result}");
                    ExitCode::SUCCESS
                }
                Err(_) => spec_error("worker_helper_refused", None),
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = (config, socket);
                spec_error("unsupported_platform", None)
            }
        }
        Command::Factory {
            config,
            lifetime_seconds,
        } => {
            #[cfg(target_os = "linux")]
            {
                if kf_sandbox::factory::serve(&config, lifetime_seconds).is_ok() {
                    ExitCode::SUCCESS
                } else {
                    spec_error("factory_refused", None)
                }
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = (config, lifetime_seconds);
                spec_error("unsupported_platform", None)
            }
        }
        #[cfg(target_os = "linux")]
        Command::FactoryChild { config, run_id } => {
            // stdout is the root factory lifeline, never a log stream.
            if kf_sandbox::factory::child(&config, &run_id).is_ok() {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(2)
            }
        }
        Command::Validate { spec } => validate_spec(spec),
        Command::CheckRuntime { config } => check_runtime(config),
        #[cfg(target_os = "linux")]
        Command::Launcher { control_socket } => {
            match kf_sandbox::launcher::entry::serve(&control_socket) {
                Ok(()) => ExitCode::from(2),
                Err(error) => spec_error(
                    "launcher_control_failed",
                    Some(serde_json::json!({
                        "io_kind": format!("{:?}", error.kind())
                    })),
                ),
            }
        }
        #[cfg(target_os = "linux")]
        Command::Bootstrap {
            parent_pid,
            initialize_namespaces,
            initialize_root,
        } => {
            let result = if initialize_namespaces {
                kf_sandbox::launcher::namespaces::bootstrap(parent_pid, initialize_root)
            } else {
                kf_sandbox::launcher::blocked::bootstrap(parent_pid)
            };
            match result {
                Ok(()) => ExitCode::from(2),
                Err(error) => spec_error(
                    "bootstrap_refused",
                    Some(serde_json::json!({"io_kind": format!("{:?}", error.kind())})),
                ),
            }
        }
        #[cfg(target_os = "linux")]
        Command::NamespaceInit {
            parent_pid,
            initialize_root,
        } => match kf_sandbox::launcher::namespaces::namespace_init(parent_pid, initialize_root) {
            Ok(()) => ExitCode::from(2),
            Err(error) => spec_error(
                "namespace_init_refused",
                Some(serde_json::json!({"io_kind":format!("{:?}",error.kind())})),
            ),
        },
    }
}

fn validate_spec(path: PathBuf) -> ExitCode {
    let Ok(file) = File::open(path) else {
        return spec_error("cannot_read_spec", None);
    };
    let mut bytes = Vec::new();
    if file
        .take(MAX_SPEC_BYTES + 1)
        .read_to_end(&mut bytes)
        .is_err()
    {
        return spec_error("cannot_read_spec", None);
    }
    if bytes.len() as u64 > MAX_SPEC_BYTES {
        return spec_error("spec_too_large", None);
    }
    let request: ExecutionSpec = match serde_json::from_slice(&bytes) {
        Ok(request) => request,
        Err(error) => {
            return spec_error(
                "invalid_spec_json",
                Some(serde_json::json!({"line": error.line(), "column": error.column()})),
            );
        }
    };
    match request.validate(&SpecPolicy::default()) {
        Ok(validated) => {
            println!(
                "{}",
                serde_json::json!({
                    "valid": true,
                    "execution_allowed": false,
                    "run_id": validated.spec().run_id.as_str()
                })
            );
            ExitCode::SUCCESS
        }
        Err(error) => spec_error("invalid_execution_spec", Some(serde_json::json!(error))),
    }
}

fn spec_error(code: &str, details: Option<serde_json::Value>) -> ExitCode {
    eprintln!("{}", serde_json::json!({"error": code, "details": details}));
    ExitCode::from(2)
}

fn check_runtime(path: PathBuf) -> ExitCode {
    let Ok(file) = File::open(path) else {
        return spec_error("cannot_read_runtime_config", None);
    };
    let mut bytes = Vec::new();
    if file
        .take(kf_sandbox::runtime::MAX_CONFIG_BYTES + 1)
        .read_to_end(&mut bytes)
        .is_err()
    {
        return spec_error("cannot_read_runtime_config", None);
    }
    if bytes.len() as u64 > kf_sandbox::runtime::MAX_CONFIG_BYTES {
        return spec_error("runtime_config_too_large", None);
    }
    let request: kf_sandbox::runtime::RuntimeConfig = match serde_json::from_slice(&bytes) {
        Ok(request) => request,
        Err(error) => {
            return spec_error(
                "invalid_runtime_config_json",
                Some(serde_json::json!({"line":error.line(), "column":error.column()})),
            );
        }
    };
    match request.validate() {
        Ok(config) => {
            let report = kf_sandbox::runtime::preflight(&config, &kf_sandbox::probe::observe());
            eprintln!("{}", serde_json::json!(report));
            ExitCode::from(2)
        }
        Err(error) => spec_error("invalid_runtime_config", Some(serde_json::json!(error))),
    }
}
