use clap::{Parser, Subcommand};
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
    }
}
