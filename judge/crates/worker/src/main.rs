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
    #[cfg(target_os = "linux")]
    ReplayDiagnosticBatch {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        job: PathBuf,
        #[arg(long)]
        artifacts: PathBuf,
        #[arg(long)]
        staging: PathBuf,
        #[arg(long)]
        admission: PathBuf,
        #[arg(long)]
        worker_id: String,
        #[arg(long)]
        factory_config_sha256: String,
        #[arg(long)]
        measure_resources: bool,
    },
    #[cfg(target_os = "linux")]
    DiagnosePublicSample {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        staging: PathBuf,
        #[arg(long)]
        admission: PathBuf,
        #[arg(long)]
        worker_id: String,
        #[arg(long)]
        factory_config_sha256: String,
    },
    CheckStopped {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        factory_config_sha256: Option<String>,
        #[arg(long, requires = "factory_config_sha256")]
        diagnostic_staging: Option<PathBuf>,
    },
    Serve {
        #[arg(long)]
        config: PathBuf,
    },
    JudgeSubmission {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        job: PathBuf,
        #[arg(long)]
        artifacts: PathBuf,
        #[arg(long)]
        staging: PathBuf,
    },
    JudgeSubmissionV2 {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        job: PathBuf,
        #[arg(long)]
        artifacts: PathBuf,
        #[arg(long)]
        staging: PathBuf,
        #[arg(long)]
        admission: PathBuf,
    },
    JudgeOnce {
        #[arg(long)]
        socket: PathBuf,
        #[arg(long)]
        job: PathBuf,
        #[arg(long)]
        artifacts: PathBuf,
    },
}
#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> ExitCode {
    let cli = Cli::parse();
    if cli.check {
        println!(
            "{}",
            serde_json::json!({"service":"judge-worker","version":env!("CARGO_PKG_VERSION"),"status":"entry_ready","contract_version":kf_contracts::SCHEMA_VERSION,"consuming_tasks":false})
        );
        return ExitCode::SUCCESS;
    }
    match cli.command {
        #[cfg(target_os = "linux")]
        Some(Command::ReplayDiagnosticBatch {
            socket,
            job,
            artifacts,
            staging,
            admission,
            worker_id,
            factory_config_sha256,
            measure_resources,
        }) => {
            let result = worker_id
                .try_into()
                .map_err(std::io::Error::other)
                .and_then(|worker| {
                    if measure_resources {
                        kf_worker::pipeline_v2::measure_diagnostic_local(
                            &socket,
                            &job,
                            &artifacts,
                            &staging,
                            &admission,
                            &worker,
                            &factory_config_sha256,
                        )
                        .map(|report| serde_json::json!(report))
                    } else {
                        kf_worker::pipeline_v2::replay_diagnostic_local(
                            &socket,
                            &job,
                            &artifacts,
                            &staging,
                            &admission,
                            &worker,
                            &factory_config_sha256,
                        )
                        .map(|report| serde_json::json!(report))
                    }
                });
            match result {
                Ok(report) => {
                    println!("{report}");
                    ExitCode::SUCCESS
                }
                Err(_) => {
                    eprintln!("diagnostic replay refused");
                    ExitCode::from(2)
                }
            }
        }
        #[cfg(target_os = "linux")]
        Some(Command::DiagnosePublicSample {
            socket,
            staging,
            admission,
            worker_id,
            factory_config_sha256,
        }) => {
            let result = worker_id
                .try_into()
                .map_err(std::io::Error::other)
                .and_then(|worker| {
                    kf_worker::diagnostic_stdio::run_stdio(
                        socket,
                        staging,
                        &admission,
                        &worker,
                        &factory_config_sha256,
                    )
                });
            if result.is_ok() {
                ExitCode::SUCCESS
            } else {
                eprintln!("diagnostic worker refused");
                ExitCode::from(2)
            }
        }
        Some(Command::CheckStopped {
            socket,
            factory_config_sha256,
            diagnostic_staging,
        }) => {
            let factory = kf_sandbox::factory::FactoryClient::new(socket);
            let proof = match factory_config_sha256 {
                Some(digest) => factory.stopped_for_configuration(&digest),
                None => factory.stopped(),
            };
            let proof = proof.and_then(|clean| {
                if let Some(staging) = diagnostic_staging.filter(|_| clean) {
                    kf_sandbox::artifacts::StagePublication::recover(&staging)?;
                    return Ok(std::fs::read_dir(staging)?.next().transpose()?.is_none());
                }
                Ok(clean)
            });
            match proof {
                Ok(clean) => {
                    println!("{}", serde_json::json!({"cleanup_complete":clean}));
                    ExitCode::SUCCESS
                }
                Err(_) => {
                    eprintln!("stop proof unavailable");
                    ExitCode::from(2)
                }
            }
        }
        Some(Command::Serve { config }) => {
            let c = match kf_worker::consumer::Config::load(&config) {
                Ok(c) => c,
                Err(_) => {
                    eprintln!("worker configuration refused");
                    return ExitCode::from(2);
                }
            };
            let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
            let watch = stop.clone();
            tokio::spawn(async move {
                let mut term =
                    tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                        .expect("signal registration");
                tokio::select! {_ = term.recv()=>{},_ = tokio::signal::ctrl_c()=>{}}
                watch.store(true, std::sync::atomic::Ordering::Release);
            });
            match tokio::task::spawn_blocking(move || kf_worker::consumer::run(c, stop)).await {
                Ok(Ok(())) => ExitCode::SUCCESS,
                _ => {
                    eprintln!("worker unavailable");
                    ExitCode::from(2)
                }
            }
        }
        Some(Command::JudgeSubmission {
            socket,
            job,
            artifacts,
            staging,
        }) => {
            #[cfg(target_os = "linux")]
            {
                match kf_worker::pipeline::judge_local(&socket, &job, &artifacts, &staging) {
                    Ok(result) => {
                        println!("{}", serde_json::json!(result));
                        ExitCode::SUCCESS
                    }
                    Err(_) => {
                        eprintln!("submission_failed");
                        ExitCode::from(2)
                    }
                }
            }
            #[cfg(not(target_os = "linux"))]
            {
                let _ = (socket, job, artifacts, staging);
                ExitCode::from(2)
            }
        }
        Some(Command::JudgeSubmissionV2 {
            socket,
            job,
            artifacts,
            staging,
            admission,
        }) => {
            let result = (|| {
                use std::os::unix::fs::{MetadataExt, PermissionsExt};
                let m = std::fs::symlink_metadata(&admission)?;
                if !m.is_file() || m.nlink() != 1 || m.permissions().mode() & 0o7777 != 0o600 {
                    return Err(std::io::Error::other("operator admission file refused"));
                }
                let policy = kf_contracts::judging::decode_wire(
                    &kf_sandbox::execution::read_bounded_file(&admission, 32768)?,
                )
                .map_err(std::io::Error::other)?;
                kf_worker::pipeline_v2::judge_local(&socket, &job, &artifacts, &staging, &policy)
            })();
            match result {
                Ok(result) => {
                    println!("{}", serde_json::json!(result));
                    ExitCode::SUCCESS
                }
                Err(_) => {
                    eprintln!("submission_failed");
                    ExitCode::from(2)
                }
            }
        }
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
