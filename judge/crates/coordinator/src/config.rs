//! Service-only secrets from a private local file; never format database/token values.
use kf_contracts::judging::{AdmissionPolicy, Identifier, LeaseToken};
use serde::Deserialize;
use std::{collections::HashSet, fs, io, net::SocketAddr, path::Path};
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkerIdentity {
    pub id: Identifier,
    pub token: LeaseToken,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub listen: SocketAddr,
    pub database_url: String,
    pub workers: Vec<WorkerIdentity>,
    pub platform_token: LeaseToken,
    pub admission: AdmissionPolicy,
    pub lease_seconds: u32,
    pub recovery_seconds: u32,
}
impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        let meta = fs::symlink_metadata(path)?;
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        if !meta.is_file()
            || meta.nlink() != 1
            || meta.permissions().mode() & 0o777 != 0o600
            || meta.len() > 32768
        {
            return Err(io::Error::other(
                "private coordinator configuration required",
            ));
        }
        let v: Self = serde_json::from_slice(&fs::read(path)?)
            .map_err(|_| io::Error::other("invalid coordinator configuration"))?;
        if !v.listen.ip().is_loopback()
            || !(5..=300).contains(&v.lease_seconds)
            || !(1..=60).contains(&v.recovery_seconds)
            || v.workers.is_empty()
            || v.workers.len() > 64
            || v.workers.iter().map(|w| w.id).collect::<HashSet<_>>().len() != v.workers.len()
            || v.workers
                .iter()
                .any(|w| w.token.expose_secret() == v.platform_token.expose_secret())
        {
            return Err(io::Error::other("invalid coordinator policy"));
        }
        let tokens: HashSet<_> = v.workers.iter().map(|w| w.token.expose_secret()).collect();
        if tokens.len() != v.workers.len() {
            return Err(io::Error::other("ambiguous worker identity"));
        }
        v.admission.validate().map_err(io::Error::other)?;
        Ok(v)
    }
}
