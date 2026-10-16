//! Finite connection slots and fixed SQL timeouts. Backend Alembic owns schema.
use std::{net::IpAddr, sync::Arc, time::Duration};
use tokio::{
    sync::{OwnedSemaphorePermit, Semaphore},
    task::JoinHandle,
};
use tokio_postgres::{Client, Config, NoTls, config::Host};
#[derive(Debug, Clone, Copy)]
pub enum Error {
    Unavailable,
    Busy,
    Rejected,
    Invalid,
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("coordinator operation refused")
    }
}
impl std::error::Error for Error {}
impl From<tokio_postgres::Error> for Error {
    fn from(_: tokio_postgres::Error) -> Self {
        Self::Unavailable
    }
}
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Clone)]
pub struct Database {
    config: Config,
    slots: Arc<Semaphore>,
}
pub struct Session {
    pub client: Client,
    task: JoinHandle<()>,
    _slot: OwnedSemaphorePermit,
}
impl Drop for Session {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Database {
    pub fn new(url: &str) -> Result<Self> {
        let mut config: Config = url.parse().map_err(|_| Error::Invalid)?;
        if config.get_hosts().is_empty()
            || config.get_hosts().iter().any(|h| match h {
                Host::Tcp(n) => !n.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback()),
                Host::Unix(p) => !p.is_absolute(),
            })
        {
            return Err(Error::Invalid);
        }
        config.connect_timeout(Duration::from_secs(3));
        config.application_name("kf-coordinator");
        config.options("-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000");
        Ok(Self {
            config,
            slots: Arc::new(Semaphore::new(4)),
        })
    }
    pub async fn session(&self) -> Result<Session> {
        let slot = self
            .slots
            .clone()
            .try_acquire_owned()
            .map_err(|_| Error::Busy)?;
        let (client, connection) = self.config.connect(NoTls).await?;
        let task = tokio::spawn(async move {
            let _ = connection.await;
        });
        let session = Session {
            client,
            task,
            _slot: slot,
        };
        let row=session.client.query_one("SELECT NOT r.rolsuper AND NOT r.rolbypassrls AND pg_has_role(current_user,'kf_oj_coordinator','MEMBER') AND NOT has_table_privilege(current_user,'accounts','SELECT') AND NOT has_table_privilege(current_user,'problems','UPDATE') FROM pg_roles r WHERE r.rolname=session_user",&[]).await?;
        if !row.get::<_, bool>(0) {
            return Err(Error::Rejected);
        }
        Ok(session)
    }
    pub async fn ready(&self) -> Result<()> {
        self.session()
            .await?
            .client
            .query("SELECT 1 FROM evaluations LIMIT 1", &[])
            .await?;
        Ok(())
    }
}
