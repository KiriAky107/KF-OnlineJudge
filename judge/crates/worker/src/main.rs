use clap::Parser;
use std::process::ExitCode;

#[derive(Parser)]
#[command(version, about = "KF judge worker entrypoint")]
struct Cli {
    /// Check the process entrypoint without consuming or executing tasks.
    #[arg(long)]
    check: bool,
}

fn main() -> ExitCode {
    if !Cli::parse().check {
        eprintln!("judge worker task loop is not implemented; use --check");
        return ExitCode::from(2);
    }
    println!(
        "{}",
        serde_json::json!({
            "service": "judge-worker",
            "version": env!("CARGO_PKG_VERSION"),
            "status": "scaffold",
            "contract_version": kf_contracts::SCHEMA_VERSION,
            "consuming_tasks": false
        })
    );
    ExitCode::SUCCESS
}
