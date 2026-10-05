use clap::Parser;
use std::net::SocketAddr;
use tokio::net::TcpListener;
use tracing_subscriber::EnvFilter;

#[derive(Parser)]
#[command(version, about = "KF judge coordinator HTTP shell")]
struct Cli {
    #[arg(long, env = "KF_COORDINATOR_LISTEN", default_value = "127.0.0.1:8100")]
    listen: SocketAddr,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();
    let listener = TcpListener::bind(Cli::parse().listen).await?;
    tracing::info!(address = %listener.local_addr()?, "coordinator scaffold listening");
    axum::serve(listener, kf_coordinator::api::router())
        .with_graceful_shutdown(async {
            if let Err(error) = tokio::signal::ctrl_c().await {
                tracing::error!(%error, "failed to listen for shutdown signal");
            }
        })
        .await?;
    Ok(())
}
