//! Ordinary transport adapter. AMQP is a bounded wake-up; claim is sole authority.
use crate::pipeline::{ArtifactPort, Attempt};
use futures_lite::{StreamExt, future::block_on};
use kf_contracts::judging_versions::{JobManifest as VersionedManifest, NodeAdmission};
use kf_contracts::{JudgeTaskNotification, SchemaVersion, judging::*};
use kf_engine::submission::{StageFailure, judge_submission};
use kf_sandbox::{artifacts::StagePublication, factory::FactoryClient};
use lapin::{Connection, ConnectionProperties, options::*, types::FieldTable};
use reqwest::blocking::Client;
use serde::Deserialize;
use sha2::{Digest as _, Sha256};
use std::{
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
const QUEUE: &str = "kf.judge.evaluations.v1";
#[derive(Debug)]
struct TransportError {
    operation: &'static str,
    category: &'static str,
}
impl std::fmt::Display for TransportError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.operation, self.category)
    }
}
impl std::error::Error for TransportError {}
fn transport_error(operation: &'static str, category: &'static str) -> io::Error {
    io::Error::other(TransportError {
        operation,
        category,
    })
}
fn error() -> io::Error {
    io::Error::other("worker operation refused")
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub worker_id: Identifier,
    pub service_token: LeaseToken,
    pub coordinator_url: String,
    pub platform_url: String,
    pub amqp_url: String,
    pub socket: PathBuf,
    pub staging: PathBuf,
    pub admission: NodeAdmission,
}
impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let m = fs::symlink_metadata(path)?;
        if !m.is_file()
            || m.nlink() != 1
            || m.permissions().mode() & 0o777 != 0o600
            || m.len() > 32768
        {
            return Err(error());
        }
        let c: Self = serde_json::from_slice(&fs::read(path)?).map_err(|_| error())?;
        for (raw, scheme) in [
            (&c.coordinator_url, "http"),
            (&c.platform_url, "http"),
            (&c.amqp_url, "amqp"),
        ] {
            let u = reqwest::Url::parse(raw).map_err(|_| error())?;
            if u.scheme() != scheme
                || !u.host_str().is_some_and(|s| {
                    s.trim_matches(['[', ']'])
                        .parse::<std::net::IpAddr>()
                        .is_ok_and(|i| i.is_loopback())
                })
                || u.query().is_some()
                || u.fragment().is_some()
                || (scheme == "http"
                    && (!u.username().is_empty() || u.password().is_some() || u.path() != "/"))
            {
                return Err(error());
            }
        }
        if !c.socket.is_absolute()
            || !c.staging.is_absolute()
            || c.admission.max_concurrent_jobs() != 1
            || c.admission.prefetch() != 1
        {
            return Err(error());
        }
        c.admission.validate().map_err(|_| error())?;
        amqp_uri(&c.amqp_url)?;
        Ok(c)
    }
}
fn amqp_uri(raw: &str) -> io::Result<lapin::uri::AMQPUri> {
    let url = reqwest::Url::parse(raw).map_err(|_| error())?;
    let host = url.host_str().ok_or_else(error)?.trim_matches(['[', ']']);
    if url.scheme() != "amqp"
        || !host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback())
    {
        return Err(error());
    }
    let mut uri: lapin::uri::AMQPUri = raw.parse().map_err(|_| error())?;
    // Locked URI parser uses domain() and otherwise substitutes localhost for IPs.
    uri.authority.host = host.into();
    // Python publisher treats an empty/root path as default vhost; normalize identically.
    if url.path().is_empty() || url.path() == "/" {
        uri.vhost = "/".into();
    }
    Ok(uri)
}
#[derive(Clone)]
struct Remote {
    client: Client,
    base: String,
    service: String,
}
impl Remote {
    fn new(c: &Config) -> io::Result<Self> {
        Ok(Self {
            client: Client::builder()
                .no_proxy()
                .redirect(reqwest::redirect::Policy::none())
                .connect_timeout(Duration::from_secs(1))
                .timeout(Duration::from_secs(2))
                .build()
                .map_err(|_| error())?,
            base: c.coordinator_url.trim_end_matches('/').into(),
            service: c.service_token.expose_secret().into(),
        })
    }
    fn post<T: serde::Serialize>(
        &self,
        route: &str,
        body: &T,
        grant: Option<&LeaseGrant>,
    ) -> io::Result<reqwest::blocking::Response> {
        let mut r = self
            .client
            .post(format!("{}{route}", self.base))
            .bearer_auth(&self.service)
            .json(body);
        if let Some(g) = grant {
            r = r.header("x-lease-token", g.lease_token.expose_secret());
        }
        r.send().map_err(|_| error())
    }
    fn bytes(mut r: reqwest::blocking::Response, max: usize) -> io::Result<Vec<u8>> {
        if !r.status().is_success() || r.content_length().is_some_and(|n| n > max as u64) {
            return Err(error());
        }
        let mut b = Vec::new();
        (&mut r).take(max as u64 + 1).read_to_end(&mut b)?;
        if b.len() > max {
            return Err(error());
        }
        Ok(b)
    }
    fn pending(&self) -> io::Result<Vec<Pending>> {
        let r = self
            .client
            .get(format!("{}/internal/pending-stops", self.base))
            .bearer_auth(&self.service)
            .send()
            .map_err(|_| transport_error("coordinator_pending", "send"))?;
        let b = Self::bytes(r, 16384)?;
        let v: Vec<Pending> = serde_json::from_slice(&b)
            .map_err(|_| transport_error("coordinator_pending", "decode"))?;
        if v.len() > 16
            || v.iter()
                .any(|p| p.case_count == 0 || p.case_count > MAX_CASES)
        {
            return Err(error());
        }
        Ok(v)
    }
    fn reconcile(&self, c: &Config) -> io::Result<bool> {
        let pending = self.pending()?;
        if pending.is_empty() {
            return Ok(true);
        }
        StagePublication::recover(&c.staging)?;
        let clean = FactoryClient::new(c.socket.clone())
            .stopped()
            .unwrap_or(false);
        for p in pending {
            if p.binding.worker_id != c.worker_id {
                return Err(error());
            }
            let r = StoppedReport {
                schema_version: SchemaVersion,
                kind: StoppedKind::Value,
                binding: p.binding,
                cleanup_complete: clean,
            };
            if self.post("/internal/stopped", &r, None)?.status().as_u16() != 204 {
                return Err(error());
            }
        }
        Ok(clean)
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Pending {
    binding: LeaseBinding,
    case_count: usize,
}
struct RemoteArtifacts {
    client: Client,
    base: String,
    grant: LeaseGrant,
}
impl RemoteArtifacts {
    fn headers(&self, r: reqwest::blocking::RequestBuilder) -> reqwest::blocking::RequestBuilder {
        r.bearer_auth(self.grant.lease_token.expose_secret())
            .header(
                "x-worker-id",
                self.grant.binding.worker_id.as_uuid().to_string(),
            )
            .header("x-fencing-version", self.grant.binding.fencing_version)
    }
    fn url(&self, suffix: &str) -> String {
        format!(
            "{}/api/v1/internal/evaluations/{}/attempts/{}{suffix}",
            self.base,
            self.grant.binding.evaluation_id.as_uuid(),
            self.grant.binding.attempt_id.as_uuid()
        )
    }
    fn manifest(&self) -> io::Result<JobManifest> {
        let response = self
            .headers(self.client.get(self.url("/manifest")))
            .send()
            .map_err(|_| error())?;
        let m: JobManifest =
            decode_wire(&Remote::bytes(response, MAX_WIRE_BYTES)?).map_err(|_| error())?;
        let digest = format!(
            "{:x}",
            Sha256::digest(manifest_bytes(&m).map_err(|_| error())?)
        );
        if digest != self.grant.manifest_digest.as_str()
            || m.evaluation_id != self.grant.binding.evaluation_id
        {
            return Err(error());
        }
        Ok(m)
    }
}
impl ArtifactPort for RemoteArtifacts {
    fn read(&mut self, reference: &ArtifactReference) -> io::Result<Vec<u8>> {
        let r = self
            .headers(
                self.client
                    .get(self.url(&format!("/artifacts/{}", reference.digest.as_str()))),
            )
            .send()
            .map_err(|_| error())?;
        let bytes = Remote::bytes(r, reference.bytes as usize)?;
        if bytes.len() as u64 != reference.bytes
            || format!("{:x}", Sha256::digest(&bytes)) != reference.digest.as_str()
        {
            return Err(error());
        }
        Ok(bytes)
    }
}
fn lease_deadline(ms: u64) -> io::Result<Instant> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| error())?
        .as_millis() as u64;
    let remaining = ms
        .checked_sub(now)
        .filter(|v| *v >= 500 && *v <= 300000)
        .ok_or_else(error)?;
    Ok(Instant::now() + Duration::from_millis(remaining))
}
fn execute(c: &Config, r: &Remote, g: LeaseGrant, stop: &AtomicBool) -> io::Result<()> {
    if g.binding.worker_id != c.worker_id {
        return Err(error());
    }
    let artifacts = RemoteArtifacts {
        client: r.client.clone(),
        base: c.platform_url.trim_end_matches('/').into(),
        grant: g.clone(),
    };
    let m = artifacts.manifest()?;
    c.admission
        .admit(&VersionedManifest::V1(m.clone()), g.attempt_number)
        .map_err(|_| error())?;
    let cancel = AtomicBool::new(false);
    let done = AtomicBool::new(false);
    let initial = lease_deadline(g.expires_at_unix_ms)?;
    std::thread::scope(|scope| {
        let renew = scope.spawn(|| {
            let mut deadline = initial;
            let mut next = Instant::now() + Duration::from_secs(1);
            while !done.load(Ordering::Acquire) {
                if stop.load(Ordering::Acquire) || Instant::now() >= deadline {
                    cancel.store(true, Ordering::Release);
                    break;
                }
                if Instant::now() >= next {
                    let result = (|| {
                        let response = r.post("/internal/renew", &g.binding, Some(&g))?;
                        let b = Remote::bytes(response, 1024)?;
                        let v: serde_json::Value =
                            serde_json::from_slice(&b).map_err(|_| error())?;
                        lease_deadline(v["expires_at_unix_ms"].as_u64().ok_or_else(error)?)
                    })();
                    match result {
                        Ok(d) => deadline = d,
                        Err(_) => {
                            cancel.store(true, Ordering::Release);
                            break;
                        }
                    }
                    next = Instant::now() + Duration::from_secs(1);
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        });
        let result = (|| {
            let mut sequence = 0u64;
            let mut adapter = Attempt::new(
                artifacts,
                c.socket.clone(),
                c.staging.clone(),
                m.job_wall_time_us,
                &cancel,
            )?
            .with_control(|stage, index| {
                sequence += 1;
                let report = StageReport {
                    schema_version: SchemaVersion,
                    kind: StageKind::Value,
                    binding: g.binding.clone(),
                    sequence,
                    stage,
                    case_index: index,
                };
                for _ in 0..2 {
                    match r.post("/internal/stage", &report, Some(&g)) {
                        Ok(response) if response.status().as_u16() == 204 => return Ok(()),
                        Ok(response) if response.status().as_u16() == 409 => break,
                        _ => {}
                    }
                    std::thread::sleep(Duration::from_millis(100));
                }
                cancel.store(true, Ordering::Release);
                Err(StageFailure {
                    code: FailureCode::LeaseLost,
                    cleanup_complete: true,
                })
            });
            let mut result = judge_submission(
                &mut adapter,
                &m,
                g.binding.clone(),
                g.manifest_digest.clone(),
            )
            .map_err(|_| error())?;
            transport_cancellation(&mut result.report);
            result
                .report
                .validate_for_manifest(&m, &g.manifest_digest)
                .map_err(|_| error())?;
            // Same immutable report only; a transport ambiguity never becomes success.
            for _ in 0..3 {
                match r.post("/internal/report", &result.report, Some(&g)) {
                    Ok(response) if response.status().as_u16() == 204 => return Ok(()),
                    Ok(response) if response.status().as_u16() == 409 => break,
                    _ => {}
                }
                std::thread::sleep(Duration::from_millis(200));
            }
            Err(error())
        })();
        done.store(true, Ordering::Release);
        renew.join().map_err(|_| error())?;
        result
    })
}
// A task stopped by service shutdown/transport loss is an infrastructure failure.
// User cancellation is authoritative in Coordinator and completes via actual stop proof.
fn transport_cancellation(report: &mut JudgementReport) {
    if report.verdict == Verdict::Cancelled {
        report.verdict = Verdict::SystemError;
        report.failure_code = Some(FailureCode::LeaseLost);
        if report.compilation.status == CompilationStatus::Cancelled {
            report.compilation.status = CompilationStatus::SystemError;
        }
    }
}
async fn bounded<T>(
    operation: &'static str,
    future: impl std::future::Future<Output = lapin::Result<T>>,
) -> io::Result<T> {
    tokio::time::timeout(Duration::from_secs(5), future)
        .await
        .map_err(|_| transport_error(operation, "deadline"))?
        .map_err(|failure| {
            transport_error(
                operation,
                match failure {
                    lapin::Error::IOError(_) => "io",
                    lapin::Error::ProtocolError(_) => "protocol",
                    lapin::Error::MissingHeartbeatError => "heartbeat",
                    _ => "refused",
                },
            )
        })
}
async fn consume(c: &Config, r: &Remote, stop: &AtomicBool) -> io::Result<()> {
    if !r.reconcile(c)? {
        return Err(error());
    }
    let connection = bounded(
        "amqp_connect",
        Connection::connect_uri(amqp_uri(&c.amqp_url)?, ConnectionProperties::default()),
    )
    .await?;
    let channel = bounded("amqp_channel", connection.create_channel()).await?;
    bounded("amqp_qos", channel.basic_qos(1, BasicQosOptions::default())).await?;
    let mut arguments = FieldTable::default();
    arguments.insert("x-max-length".into(), 64i32.into());
    arguments.insert("x-max-length-bytes".into(), 32768i32.into());
    arguments.insert(
        "x-overflow".into(),
        lapin::types::AMQPValue::LongString("reject-publish".into()),
    );
    arguments.insert("x-message-ttl".into(), 600000i32.into());
    bounded(
        "amqp_declare",
        channel.queue_declare(
            QUEUE,
            QueueDeclareOptions {
                durable: true,
                ..Default::default()
            },
            arguments,
        ),
    )
    .await?;
    let mut consumer = bounded(
        "amqp_consume",
        channel.basic_consume(
            QUEUE,
            "kf-worker",
            BasicConsumeOptions::default(),
            FieldTable::default(),
        ),
    )
    .await?;
    while !stop.load(Ordering::Acquire) {
        // A bounded read wait also reconciles revoked/expired attempts with no message.
        let delivery = tokio::time::timeout(Duration::from_secs(1), consumer.next()).await;
        let delivery = match delivery {
            Err(_) => {
                if !r.reconcile(c)? {
                    return Err(error());
                }
                continue;
            }
            Ok(None) => break,
            Ok(Some(v)) => v.map_err(|_| transport_error("amqp_read", "refused"))?,
        };
        let notice: JudgeTaskNotification = match decode_wire(&delivery.data) {
            Ok(v) => v,
            Err(_) => {
                bounded(
                    "amqp_reject",
                    delivery.reject(BasicRejectOptions { requeue: false }),
                )
                .await?;
                continue;
            }
        };
        let grant = r.post("/internal/claim", &notice, None)?;
        match grant.status().as_u16() {
            200 => {
                let g: LeaseGrant =
                    decode_wire(&Remote::bytes(grant, MAX_WIRE_BYTES)?).map_err(|_| error())?;
                let result = execute(c, r, g, stop);
                if result.is_err() && !r.reconcile(c)? {
                    return Err(error());
                }
                // If report was unconfirmed, DB expiry/recovery owns the next notification.
                bounded("amqp_ack", delivery.ack(BasicAckOptions::default())).await?;
            }
            204 | 429 => bounded("amqp_ack", delivery.ack(BasicAckOptions::default())).await?,
            _ => return Err(error()),
        }
    }
    bounded("amqp_close", connection.close(200, "worker stopped")).await?;
    Ok(())
}
pub fn run(c: Config, stop: Arc<AtomicBool>) -> io::Result<()> {
    StagePublication::recover(&c.staging)?;
    let remote = Remote::new(&c)?;
    let mut failures = 0u32;
    while !stop.load(Ordering::Acquire) {
        let result = block_on(consume(&c, &remote, &stop));
        if stop.load(Ordering::Acquire) {
            break;
        }
        if result.is_ok() {
            failures = 0;
        } else {
            failures = failures.saturating_add(1).min(5);
            let code = result
                .err()
                .and_then(|e| e.into_inner())
                .and_then(|e| e.downcast::<TransportError>().ok());
            if let Some(code) = code {
                eprintln!("worker transport unavailable: {code}");
            } else {
                eprintln!("worker transport unavailable: refused");
            }
        }
        let deadline = Instant::now() + Duration::from_secs(2u64.pow(failures).min(30));
        while Instant::now() < deadline && !stop.load(Ordering::Acquire) {
            std::thread::sleep(Duration::from_millis(50));
        }
    }
    // A still-live lease may take a bounded expiry interval to become pending.
    let _ = remote.reconcile(&c);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn amqp_numeric_authority_and_default_vhost_are_preserved() {
        for (raw, host, vhost) in [
            ("amqp://fixture:temporary@127.0.0.2:5672/", "127.0.0.2", "/"),
            (
                "amqp://fixture:temporary@127.0.0.1:5672/%2f",
                "127.0.0.1",
                "/",
            ),
            ("amqp://fixture:temporary@[::1]:5672/named", "::1", "named"),
        ] {
            let uri = amqp_uri(raw).unwrap();
            assert_eq!(uri.authority.host, host);
            assert_eq!(uri.vhost, vhost);
        }
        assert!(amqp_uri("amqp://fixture:temporary@example.invalid/").is_err());
        assert!(amqp_uri("amqp://fixture:temporary@192.0.2.1/").is_err());
    }
    #[test]
    fn stopped_transport_cannot_claim_user_cancellation() {
        let mut report: JudgementReport = decode_wire(include_bytes!(
            "../../../../tests/fixtures/contracts/judge-result.valid.json"
        ))
        .unwrap();
        transport_cancellation(&mut report);
        assert_eq!(report.verdict, Verdict::Accepted);
        assert_eq!(report.failure_code, None);
        report.cases.last_mut().unwrap().verdict = Verdict::Cancelled;
        report.verdict = Verdict::Cancelled;
        report.validate().unwrap();
        transport_cancellation(&mut report);
        assert_eq!(report.failure_code, Some(FailureCode::LeaseLost));
        assert_eq!(report.verdict, Verdict::SystemError);
        report.validate().unwrap();
        report.cases.clear();
        report.run_cpu_time_us = None;
        report.run_memory_peak_bytes = None;
        report.compilation.program = None;
        report.compilation.status = CompilationStatus::Cancelled;
        report.verdict = Verdict::Cancelled;
        report.failure_code = None;
        report.validate().unwrap();
        transport_cancellation(&mut report);
        assert_eq!(report.compilation.status, CompilationStatus::SystemError);
        report.validate().unwrap();
    }
}
