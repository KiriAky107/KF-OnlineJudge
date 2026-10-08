use clap::{Parser, Subcommand};
use std::{path::PathBuf, process::ExitCode};
#[derive(Parser)]
#[command(
    version,
    about = "KF ordinary judge worker",
    args_conflicts_with_subcommands = true
)]
struct Cli {
    #[arg(long)]
    check: bool,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Subcommand)]
enum Command {
    JudgeOnce {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        job: PathBuf,
        #[arg(long)]
        artifacts: PathBuf,
    },
}
fn main() -> ExitCode {
    let cli = Cli::parse();
    if cli.check {
        println!(
            "{}",
            serde_json::json!({"service":"judge-worker","version":env!("CARGO_PKG_VERSION"),"status":"entry_ready","contract_version":kf_contracts::SCHEMA_VERSION,"consuming_tasks":false})
        );
        return ExitCode::SUCCESS;
    }
    match cli.command {
        Some(Command::JudgeOnce {
            socket,
            job,
            artifacts,
        }) => {
            #[cfg(target_os = "linux")]
            match kf_worker::native::judge_once(&socket, &job, &artifacts) {
                Ok(result) => {
                    println!("{}", serde_json::json!(result));
                    ExitCode::SUCCESS
                }
                Err(_) => {
                    eprintln!("{}", serde_json::json!({"error":"judge_request_failed"}));
                    ExitCode::from(2)
                }
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = (socket, job, artifacts);
                eprintln!("unsupported worker platform");
                ExitCode::from(2)
            }
        }
        None => {
            eprintln!("worker consumer loop unavailable; use --check or judge-once");
            ExitCode::from(2)
        }
    }
}
