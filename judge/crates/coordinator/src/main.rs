use clap::Parser;
use kf_coordinator::{
    api::{App, application, router},
    config::Config,
    database::Database,
    repository::Repository,
};
use std::{net::SocketAddr, path::PathBuf};
use tokio::net::TcpListener;
#[derive(Parser)]
#[command(version, about = "KF persistent judge coordinator")]
struct Cli {
    #[arg(long, env = "KF_COORDINATOR_CONFIG")]
    config: Option<PathBuf>,
    #[arg(long, env = "KF_COORDINATOR_LISTEN", default_value = "127.0.0.1:8100")]
    listen: SocketAddr,
    /// One bounded, operator-only repair from an already epoch-bound final report.
    #[arg(long, requires = "config")]
    backfill_performance: Option<uuid::Uuid>,
}
async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let cli = Cli::parse();
    let mut task = None;
    let (listen, app) = if let Some(path) = cli.config {
        let c = Config::load(&path)?;
        let listen = c.listen;
        let repository = Repository {
            database: Database::new(&c.database_url)?,
            admission: c.admission.clone(),
            lease_seconds: c.lease_seconds,
            measurement_epochs: c
                .workers
                .iter()
                .filter_map(|w| w.measurement_epoch.clone().map(|epoch| (w.id, epoch)))
                .collect(),
        };
        if let Some(evaluation) = cli.backfill_performance {
            let evaluation = serde_json::from_value(serde_json::json!(evaluation))?;
            let created = repository.backfill_performance(evaluation).await?;
            println!("trusted performance rows created: {created}");
            return Ok(());
        }
        let r = repository.clone();
        let period = c.recovery_seconds;
        task = Some(tokio::spawn(async move {
            let mut tick = tokio::time::interval(std::time::Duration::from_secs(u64::from(period)));
            tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                tick.tick().await;
                let _ = r.recover().await;
            }
        }));
        (listen, application(App::new(repository, c)))
    } else {
        (cli.listen, router())
    };
    let listener = TcpListener::bind(listen).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(async {
            let mut term =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("signal registration");
            tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{}}
        })
        .await?;
    if let Some(t) = task {
        t.abort();
    }
    Ok(())
}
#[tokio::main]
async fn main() -> std::process::ExitCode {
    match run().await {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(_) => {
            eprintln!("coordinator unavailable");
            std::process::ExitCode::from(2)
        }
    }
}
