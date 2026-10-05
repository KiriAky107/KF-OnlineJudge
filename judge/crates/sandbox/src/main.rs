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
    /// Reserved execution entrypoint; refuses execution until isolation is implemented.
    Run,
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
        Command::Run => {
            eprintln!(
                "{{\"error\":\"sandbox_not_implemented\",\"message\":\"isolated execution is unavailable\"}}"
            );
            ExitCode::from(2)
        }
        Command::Validate { spec } => validate_spec(spec),
        Command::CheckRuntime { config } => check_runtime(config),
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
