#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use kf_sandbox::{
    artifacts::ArtifactRoot,
    factory::FactoryClient,
    outcome::{CleanupState, ExecutionOutcome},
    spec::ExecutionSpec,
};
use sha2::{Digest as _, Sha256};
use std::{
    fs,
    io::{Read, Write},
    os::unix::{
        fs::{PermissionsExt, chown},
        net::UnixStream,
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
    time::{Duration, Instant},
};
static COUNT: AtomicU32 = AtomicU32::new(0);
const SOURCE:&[u8]=br#"#include <stdio.h>
#include <unistd.h>
#include <fcntl.h>
int main(void){ unsigned char c; FILE *f=fopen("previous","rb"); if(f) return 91; f=fopen("previous","wb"); if(!f)return 92; fclose(f); if(access("expected",F_OK)==0)return 93; while(fread(&c,1,1,stdin)){if(c=='!'){fputs("live",stdout);fflush(stdout);for(;;){}} if(c=='#')return 7; fwrite(&c,1,1,stdout);} return 0; }
"#;
fn worker_file(p: &Path) {
    chown(p, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(p, fs::Permissions::from_mode(0o600)).unwrap();
}
fn id(n: u32) -> String {
    format!("aaaaaaaa-aaaa-4aaa-8aaa-{n:012}")
}
fn blob(dir: &Path, data: &[u8]) -> serde_json::Value {
    let hash = format!("{:x}", Sha256::digest(data));
    let path = dir.join(&hash);
    if !path.exists() {
        fs::write(&path, data).unwrap();
        worker_file(&path);
    }
    serde_json::json!({"digest":hash,"bytes":data.len()})
}
struct Fixture {
    root: support::Root,
    dir: PathBuf,
    config: PathBuf,
    socket: PathBuf,
    factory: Option<Child>,
}
impl Fixture {
    fn new() -> Self {
        support::require_vm();
        let mut root = support::Root::new();
        root.config.max_concurrent_runs = 1;
        root.config.identities.count = 1;
        root.config.aggregate.task_cpu_quota_us = 100000;
        let dir = PathBuf::from(format!(
            "/kf/pipeline-{}-{}",
            std::process::id(),
            COUNT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o711)).unwrap();
        let config = dir.join("runtime.json");
        fs::write(&config, serde_json::to_vec(&root.config).unwrap()).unwrap();
        fs::set_permissions(&config, fs::Permissions::from_mode(0o600)).unwrap();
        for n in ["artifacts", "staging"] {
            let p = dir.join(n);
            fs::create_dir(&p).unwrap();
            chown(&p, Some(1000), Some(1000)).unwrap();
            fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
        }
        let socket = PathBuf::from("/var/lib/kf-judge/factory/control.sock");
        Self {
            root,
            dir,
            config,
            socket,
            factory: None,
        }
    }
    fn start(&mut self) {
        use std::os::unix::fs::MetadataExt;
        let previous = fs::symlink_metadata(&self.socket).ok().map(|m| m.ino());
        let mut child = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
            .env_clear()
            .args(["factory", "--config"])
            .arg(&self.config)
            .args(["--lifetime-seconds", "180"])
            .stdout(Stdio::null())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(15);
        loop {
            assert!(
                child.try_wait().unwrap().is_none(),
                "factory exited before ready"
            );
            if self.socket.exists()
                && fs::metadata(&self.socket).unwrap().permissions().mode() & 0o777 == 0o660
                && previous != Some(fs::metadata(&self.socket).unwrap().ino())
            {
                break;
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        let own = fs::read_to_string(format!("/proc/{}/cgroup", child.id())).unwrap();
        let group = PathBuf::from(format!(
            "/sys/fs/cgroup{}",
            own.trim().strip_prefix("0::").unwrap()
        ));
        let values: serde_json::Value = ["memory.max", "memory.swap.max", "pids.max", "cpu.max"]
            .iter()
            .map(|n| {
                (
                    n.to_string(),
                    serde_json::Value::String(
                        fs::read_to_string(group.join(n)).unwrap().trim().into(),
                    ),
                )
            })
            .collect::<serde_json::Map<_, _>>()
            .into();
        assert_eq!(
            values,
            serde_json::json!({"memory.max":"268435456","memory.swap.max":"0","pids.max":"32","cpu.max":"50000 100000"})
        );
        println!(
            "KF_FACTORY_BUDGET_PROOF {}",
            serde_json::json!({"pid":child.id(),"actual_factory_limits":values,"slots":1,"lifetime_seconds":180})
        );
        let deadline = Instant::now() + Duration::from_secs(5);
        while fs::read_to_string(self.root.path.join("memory.max"))
            .unwrap()
            .trim()
            != self.root.config.aggregate.memory_bytes.to_string()
        {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        let total: serde_json::Value = [
            "memory.max",
            "memory.swap.max",
            "pids.max",
            "cpu.max",
            "cgroup.max.depth",
            "cgroup.max.descendants",
        ]
        .iter()
        .map(|n| {
            (
                n.to_string(),
                serde_json::Value::String(
                    fs::read_to_string(self.root.path.join(n))
                        .unwrap()
                        .trim()
                        .into(),
                ),
            )
        })
        .collect::<serde_json::Map<_, _>>()
        .into();
        assert_eq!(
            total,
            serde_json::json!({"memory.max":self.root.config.aggregate.memory_bytes.to_string(),"memory.swap.max":"0","pids.max":self.root.config.aggregate.processes.to_string(),"cpu.max":format!("{} {}",self.root.config.aggregate.cpu_quota_us,self.root.config.aggregate.cpu_period_us),"cgroup.max.depth":"1","cgroup.max.descendants":"1"})
        );
        println!("KF_FACTORY_TOTAL_PROOF {}", total);
        self.factory = Some(child);
    }
    fn job(&self, language: &str, source: &[u8], inputs: &[&[u8]], answers: &[&[u8]]) -> PathBuf {
        let n = COUNT.fetch_add(1, Ordering::Relaxed) + 100;
        let mut m: serde_json::Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/contracts/judge-manifest.valid.json"
        ))
        .unwrap();
        let a = self.dir.join("artifacts");
        m["language"] = language.into();
        m["source"] = blob(&a, source);
        m["cases"]=serde_json::Value::Array(inputs.iter().zip(answers).enumerate().map(|(i,(input,expected))|serde_json::json!({"case_id":id(1000+i as u32),"input":blob(&a,input),"expected":blob(&a,expected)})).collect());
        m["job_wall_time_us"] = 300_000_000u64.into();
        let parsed: kf_contracts::judging::JobManifest = serde_json::from_value(m.clone()).unwrap();
        let hash = format!(
            "{:x}",
            Sha256::digest(kf_contracts::judging::manifest_bytes(&parsed).unwrap())
        );
        let path = self.dir.join(format!("job-{n}.json"));
        fs::write(&path,serde_json::to_vec(&serde_json::json!({"manifest":m,"manifest_digest":hash,"binding":{"evaluation_id":parsed.evaluation_id,"attempt_id":id(n),"worker_id":id(999),"fencing_version":1}})).unwrap()).unwrap();
        worker_file(&path);
        path
    }
    fn worker(&self, job: &Path) -> Child {
        Command::new(std::env::var_os("KF_WORKER_TEST_BIN").unwrap())
            .env_clear()
            .args(["judge-submission", "--socket"])
            .arg(&self.socket)
            .arg("--job")
            .arg(job)
            .arg("--artifacts")
            .arg(self.dir.join("artifacts"))
            .arg("--staging")
            .arg(self.dir.join("staging"))
            .uid(1000)
            .gid(1000)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap()
    }
    fn wait_empty(&self) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            let empty = ["tasks", "identities", "captures"].iter().all(|n| {
                let p = PathBuf::from(format!("/var/lib/kf-judge/{n}"));
                !p.exists() || fs::read_dir(p).unwrap().count() == 0
            });
            let groups = fs::read_dir(&self.root.path)
                .unwrap()
                .all(|e| !e.unwrap().file_type().unwrap().is_dir());
            if empty && groups {
                break;
            }
            assert!(
                Instant::now() < deadline,
                "owned task resources did not clear"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    fn run(&self, job: &Path, verdict: &str) -> serde_json::Value {
        let output = self.worker(job).wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let value: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(value["result"]["report"]["verdict"], verdict, "{value}");
        assert_eq!(value["result"]["report"]["cleanup_complete"], true);
        self.wait_empty();
        assert_eq!(fs::read_dir(self.dir.join("staging")).unwrap().count(), 0);
        println!(
            "KF_PIPELINE_PROOF {}",
            serde_json::json!({"worker_uid":1000,"worker_gid":1000,"value":value})
        );
        value
    }
    fn live(&self) {
        let path = PathBuf::from("/var/lib/kf-judge/identities/uid-100000/owner.json");
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            if let Ok(text) = fs::read_to_string(&path)
                && text.contains("r-")
            {
                let parsed: serde_json::Value = serde_json::from_str(&text).unwrap();
                let pid = parsed["owner"]["pid"].as_u64().unwrap();
                let prefix = format!(
                    "/var/lib/kf-judge/captures/kf-{}",
                    parsed["run_id"].as_str().unwrap()
                );
                for e in fs::read_dir(format!("/proc/{pid}/fd")).unwrap().flatten() {
                    if fs::read_link(e.path())
                        .is_ok_and(|p| p.to_string_lossy().starts_with(&prefix))
                        && fs::metadata(e.path()).is_ok_and(|m| m.is_file() && m.len() >= 4)
                    {
                        let mut bytes = [0; 4];
                        if fs::File::open(e.path())
                            .unwrap()
                            .read_exact(&mut bytes)
                            .is_ok()
                            && bytes == *b"live"
                        {
                            let status = fs::read_to_string(format!("/proc/{pid}/status")).unwrap();
                            assert!(status.contains("Uid:\t0\t0\t0\t0"));
                            assert!(status.contains("Threads:\t1"));
                            assert!(fs::read(format!("/proc/{pid}/environ")).unwrap().is_empty());
                            println!(
                                "KF_FACTORY_LIVE_PROOF {}",
                                serde_json::json!({"pid":pid,"uid":0,"threads":1,"environment_empty":true,"stdout":"live","run_id":parsed["run_id"]})
                            );
                            return;
                        }
                    }
                }
            }
            assert!(Instant::now() < deadline, "runtime task not reached");
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    fn probe(&self, mode: &str, run_id: &str) -> serde_json::Value {
        let o = Command::new(std::env::current_exe().unwrap())
            .env_clear()
            .args([
                "--ignored",
                "--exact",
                "ordinary_control_probe",
                "--test-threads=1",
                "--nocapture",
            ])
            .env("KF_FACTORY_PROBE", mode)
            .env("KF_FACTORY_SOCKET", &self.socket)
            .env("KF_FACTORY_INPUTS", self.dir.join("control-inputs"))
            .env("KF_FACTORY_RUN", run_id)
            .uid(1000)
            .gid(1000)
            .output()
            .unwrap();
        assert!(
            o.status.success(),
            "{} {}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        );
        let text = String::from_utf8(o.stdout).unwrap();
        let value: serde_json::Value = serde_json::from_str(
            text.lines()
                .find(|l| l.contains("KF_FACTORY_CONTROL_PROOF "))
                .unwrap()
                .split_once("KF_FACTORY_CONTROL_PROOF ")
                .unwrap()
                .1,
        )
        .unwrap();
        println!("KF_FACTORY_CONTROL_PROOF {value}");
        value
    }
    fn control_inputs(&self) {
        let p = self.dir.join("control-inputs");
        fs::create_dir(&p).unwrap();
        chown(&p, Some(1000), Some(1000)).unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
        fs::copy("/kf/bin/worker-native", p.join("program")).unwrap();
        fs::write(p.join("stdin"), b"!").unwrap();
        for n in ["program", "stdin"] {
            worker_file(&p.join(n));
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Some(mut child) = self.factory.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        let _ = fs::remove_dir_all(&self.dir);
    }
}
#[test]
#[ignore = "independent capped VM; ordinary authenticated probe child only"]
fn ordinary_control_probe() {
    let Ok(mode) = std::env::var("KF_FACTORY_PROBE") else {
        return;
    };
    let socket = PathBuf::from(std::env::var_os("KF_FACTORY_SOCKET").unwrap());
    let run_id = std::env::var("KF_FACTORY_RUN").unwrap();
    let publication =
        ArtifactRoot::open(Path::new(&std::env::var_os("KF_FACTORY_INPUTS").unwrap())).unwrap();
    let mut spec: ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    spec.run_id = run_id.clone();
    spec.inputs[0].artifact_id = "program".into();
    spec.stdin_artifact = Some("stdin".into());
    let client = FactoryClient::new(socket.clone());
    match mode.as_str() {
        "cancel" => {
            let cancelled = AtomicBool::new(false);
            let (result, pid) = std::thread::scope(|s| {
                s.spawn(|| {
                    std::thread::sleep(Duration::from_millis(800));
                    cancelled.store(true, Ordering::Release)
                });
                client
                    .execute_controlled(spec.clone(), &publication, None, Some(&cancelled))
                    .unwrap()
            });
            assert_eq!(result.report.cleanup, CleanupState::Complete);
            assert_eq!(
                result
                    .report
                    .classify(&spec.validate(&Default::default()).unwrap())
                    .unwrap(),
                ExecutionOutcome::Cancelled
            );
            println!(
                "KF_FACTORY_CONTROL_PROOF {}",
                serde_json::json!({"kind":"cancel","run_id":run_id,"pid":pid,"report":result.report})
            );
        }
        "replay" => {
            assert!(client.execute(spec, &publication).is_err());
            println!(
                "KF_FACTORY_CONTROL_PROOF {}",
                serde_json::json!({"kind":"replay","run_id":run_id,"refused":true})
            );
        }
        "capacity" => {
            let mut first = UnixStream::connect(&socket).unwrap();
            raw_send(&mut first, &serde_json::json!({"run_id":run_id}));
            let ready = raw_receive(&mut first);
            assert_eq!(ready["event"], "ready");
            let mut second = UnixStream::connect(&socket).unwrap();
            raw_send(&mut second, &serde_json::json!({"run_id":"capacity-extra"}));
            assert_eq!(raw_receive(&mut second)["event"], "refused");
            drop(first);
            drop(second);
            println!(
                "KF_FACTORY_CONTROL_PROOF {}",
                serde_json::json!({"kind":"capacity","slot_limit":1,"extra_refused":true,"queued_disconnect":true})
            );
        }
        "extra" => {
            use rustix::fd::AsFd;
            use rustix::net::{SendAncillaryBuffer, SendAncillaryMessage, SendFlags};
            let mut stream = UnixStream::connect(&socket).unwrap();
            raw_send(&mut stream, &serde_json::json!({"run_id":run_id}));
            assert_eq!(raw_receive(&mut stream)["event"], "ready");
            raw_send(&mut stream, &serde_json::json!({"spec":spec}));
            let mut space = [std::mem::MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(1))];
            let borrowed = [publication.as_fd()];
            let mut ancillary = SendAncillaryBuffer::new(&mut space);
            assert!(ancillary.push(SendAncillaryMessage::ScmRights(&borrowed)));
            assert_eq!(
                rustix::net::sendmsg(
                    &stream,
                    &[std::io::IoSlice::new(&[0xa5])],
                    &mut ancillary,
                    SendFlags::NOSIGNAL
                )
                .unwrap(),
                1
            );
            raw_send(
                &mut stream,
                &serde_json::json!({"run_id":"unrequested-frame"}),
            );
            let reply = raw_receive(&mut stream);
            assert_eq!(reply["event"], "completed");
            assert!(!reply["report"]["errors"].as_array().unwrap().is_empty());
            assert_eq!(reply["report"]["stdout"]["retained_bytes"], 0);
            assert_eq!(reply["report"]["cleanup"], "complete");
            println!(
                "KF_FACTORY_CONTROL_PROOF {}",
                serde_json::json!({"kind":"extra_frame","refused_before_user_exec":true,"report":reply["report"]})
            );
        }
        "binding" => {
            let mut stream = UnixStream::connect(&socket).unwrap();
            raw_send(&mut stream, &serde_json::json!({"run_id":run_id}));
            assert_eq!(raw_receive(&mut stream)["event"], "ready");
            spec.run_id = "wrong-bound-run".into();
            raw_send(&mut stream, &serde_json::json!({"spec":spec}));
            let mut buf = [0];
            assert_eq!(stream.read(&mut buf).unwrap_or(0), 0);
            println!(
                "KF_FACTORY_CONTROL_PROOF {}",
                serde_json::json!({"kind":"binding","wrong_run_refused":true})
            );
        }
        _ => panic!("unknown fixed probe"),
    }
}
fn raw_send(s: &mut UnixStream, v: &serde_json::Value) {
    s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    let b = serde_json::to_vec(v).unwrap();
    s.write_all(&[b'K', b'F', 1, 0]).unwrap();
    s.write_all(&(b.len() as u32).to_be_bytes()).unwrap();
    s.write_all(&b).unwrap();
}
fn raw_receive(s: &mut UnixStream) -> serde_json::Value {
    let mut head = [0; 8];
    s.read_exact(&mut head).unwrap();
    let n = u32::from_be_bytes(head[4..].try_into().unwrap()) as usize;
    assert!(n <= 1024);
    let mut b = vec![0; n];
    s.read_exact(&mut b).unwrap();
    serde_json::from_slice(&b).unwrap()
}
#[test]
#[ignore = "independent capped VM: real compile-once C17/CPP20, 3 isolated points and structured CE/WA/RE"]
fn compilation_and_three_points() {
    let mut f = Fixture::new();
    f.start();
    for language in ["c17", "cpp20"] {
        let j = f.job(
            language,
            SOURCE,
            &[b"", &[0, 255, 10], b"abc\n"],
            &[b"", &[0, 255, 10], b"abc\n"],
        );
        let v = f.run(&j, "accepted");
        let stages = v["stages"].as_array().unwrap();
        assert_eq!(stages.len(), 4);
        let mut pids = std::collections::HashSet::new();
        for s in stages {
            assert!(pids.insert(s["helper_pid"].as_u64().unwrap()));
            assert_eq!(s["report"]["cleanup"], "complete");
        }
        assert_eq!(v["result"]["report"]["cases"].as_array().unwrap().len(), 3);
        assert_eq!(v["result"]["report"]["compilation"]["status"], "succeeded");
    }
    for (source, input, answer, verdict) in [
        (
            b"invalid C source".as_slice(),
            b"".as_slice(),
            b"".as_slice(),
            "compilation_error",
        ),
        (SOURCE, b"binary\n", b"binary", "wrong_answer"),
        (SOURCE, b"#", b"", "runtime_error"),
    ] {
        let j = f.job(
            "c17",
            source,
            &[input, input, input],
            &[answer, answer, answer],
        );
        let v = f.run(&j, verdict);
        if verdict == "compilation_error" {
            assert_eq!(v["stages"].as_array().unwrap().len(), 1);
            assert!(
                !v["result"]["report"]["compilation"]["diagnostics"]
                    .as_str()
                    .unwrap()
                    .is_empty()
            );
            assert_eq!(v["result"]["skipped"].as_array().unwrap().len(), 3);
        }
    }
    let j = f.job("c17", SOURCE, &[b"a", b"b", b"c"], &[b"a", b"b", b"c"]);
    let mut job: serde_json::Value = serde_json::from_slice(&fs::read(&j).unwrap()).unwrap();
    job["manifest"]["compile_limits"]["cpu_time_us"] = 10_000_000.into();
    job["manifest"]["compile_limits"]["wall_time_us"] = 1000.into();
    let m: kf_contracts::judging::JobManifest =
        serde_json::from_value(job["manifest"].clone()).unwrap();
    job["manifest_digest"] = format!(
        "{:x}",
        Sha256::digest(kf_contracts::judging::manifest_bytes(&m).unwrap())
    )
    .into();
    fs::write(&j, serde_json::to_vec(&job).unwrap()).unwrap();
    worker_file(&j);
    let v = f.run(&j, "compilation_error");
    assert_eq!(
        v["result"]["report"]["compilation"]["status"],
        "compilation_limit"
    );
    assert_eq!(v["stages"].as_array().unwrap().len(), 1);
}
#[test]
#[ignore = "independent capped VM: actual capacity/replay/binding/cancel and factory crash recovery"]
fn factory_lifecycle_and_refusal() {
    let mut f = Fixture::new();
    f.start();
    f.control_inputs();
    for (mode, run) in [
        ("capacity", "capacity-one"),
        ("binding", "binding-one"),
        ("extra", "extra-one"),
        ("cancel", "cancel-one"),
        ("replay", "cancel-one"),
    ] {
        f.probe(mode, run);
        f.wait_empty();
        std::thread::sleep(Duration::from_millis(30));
    }
    let mut root_client = UnixStream::connect(&f.socket).unwrap();
    let body = serde_json::to_vec(&serde_json::json!({"run_id":"root-forged-peer"})).unwrap();
    let mut frame = vec![b'K', b'F', 1, 0];
    frame.extend((body.len() as u32).to_be_bytes());
    frame.extend(body);
    root_client
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    if let Err(e) = root_client.write_all(&frame) {
        assert!(matches!(
            e.kind(),
            std::io::ErrorKind::BrokenPipe | std::io::ErrorKind::ConnectionReset
        ));
    }
    let mut byte = [0u8; 1];
    match root_client.read(&mut byte) {
        Ok(0) => {}
        Err(e)
            if matches!(
                e.kind(),
                std::io::ErrorKind::ConnectionReset | std::io::ErrorKind::BrokenPipe
            ) => {}
        other => panic!("unexpected root peer result {other:?}"),
    }
    drop(root_client);
    println!("KF_FACTORY_AUTH_PROOF wrong_uid_valid_request_refused=1");
    std::thread::sleep(Duration::from_millis(30));
    assert!(f.factory.as_mut().unwrap().try_wait().unwrap().is_none());
    let job = f.job("c17", SOURCE, &[b"!", b"a", b"b"], &[b"live", b"a", b"b"]);
    let worker = f.worker(&job);
    f.live();
    let mut old = f.factory.take().unwrap();
    old.kill().unwrap();
    old.wait().unwrap();
    let output = worker.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(v["result"]["report"]["verdict"], "cancelled", "{v}");
    assert_eq!(v["result"]["report"]["cleanup_complete"], true);
    assert_eq!(v["result"]["report"]["cases"].as_array().unwrap().len(), 1);
    assert_eq!(v["result"]["skipped"].as_array().unwrap().len(), 2);
    println!("KF_FACTORY_RESTART_PROOF {v}");
    f.wait_empty();
    f.start();
    f.probe("replay", "cancel-one");
    let j = f.job("c17", SOURCE, &[b"a", b"b", b"c"], &[b"a", b"b", b"c"]);
    f.run(&j, "accepted");
}

#[test]
#[ignore = "independent capped VM: disconnect, initialization failure, retained unknown lease and foreign socket"]
fn faults_preserve_foreign_resources_and_recover_owned() {
    let mut f = Fixture::new();
    f.start();
    // Kernel-authenticated Worker disappears while actual user code is live.
    let j = f.job("c17", SOURCE, &[b"!", b"a", b"b"], &[b"live", b"a", b"b"]);
    let mut worker = f.worker(&j);
    f.live();
    worker.kill().unwrap();
    worker.wait().unwrap();
    f.wait_empty();
    println!(
        "KF_FACTORY_FAULT_PROOF {}",
        serde_json::json!({"kind":"worker_disconnect","owned_resources_zero":true})
    );
    // Root-owned image publication is invalidated before preparation. No exec.
    let work = PathBuf::from(&f.root.config.image_root).join("work");
    let moved = work.with_file_name("work-operator-held");
    fs::rename(&work, &moved).unwrap();
    let j = f.job("c17", SOURCE, &[b"a", b"b", b"c"], &[b"a", b"b", b"c"]);
    let o = f.worker(&j).wait_with_output().unwrap();
    assert!(o.status.success());
    let v: serde_json::Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(v["result"]["report"]["verdict"], "system_error");
    assert!(
        v["result"]["report"]["cases"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    fs::rename(&moved, &work).unwrap();
    f.wait_empty();
    println!(
        "KF_FACTORY_FAULT_PROOF {}",
        serde_json::json!({"kind":"initialization","no_test_points_started":true,"owned_resources_zero":true})
    );
    // Unknown identity-slot entry prevents UID release/reuse even after tree stop.
    let j = f.job("c17", SOURCE, &[b"!", b"a", b"b"], &[b"live", b"a", b"b"]);
    let mut worker = f.worker(&j);
    f.live();
    let slot = PathBuf::from("/var/lib/kf-judge/identities/uid-100000");
    let foreign = slot.join("foreign-owner-content");
    fs::write(&foreign, b"preserve me").unwrap();
    worker.kill().unwrap();
    worker.wait().unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        if let Some(status) = f.factory.as_mut().unwrap().try_wait().unwrap() {
            assert!(!status.success());
            break;
        }
        assert!(Instant::now() < deadline, "fault factory did not stop");
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(fs::read(&foreign).unwrap(), b"preserve me");
    assert!(slot.exists());
    f.factory.take();
    println!(
        "KF_FACTORY_FAULT_PROOF {}",
        serde_json::json!({"kind":"cleanup_failure","foreign_preserved":true,"uid_lease_retained":true,"factory_stopped":true})
    );
    fs::remove_file(&foreign).unwrap();
    f.start();
    f.wait_empty();
    let j = f.job("c17", SOURCE, &[b"a", b"b", b"c"], &[b"a", b"b", b"c"]);
    f.run(&j, "accepted");
    // Stale socket recovery is inode-bound; replacement content is never adopted.
    let mut child = f.factory.take().unwrap();
    child.kill().unwrap();
    child.wait().unwrap();
    let saved = f.socket.with_file_name("operator-held.sock");
    fs::rename(&f.socket, &saved).unwrap();
    fs::write(&f.socket, b"foreign socket replacement").unwrap();
    let o = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
        .env_clear()
        .args(["factory", "--config"])
        .arg(&f.config)
        .args(["--lifetime-seconds", "1"])
        .output()
        .unwrap();
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(fs::read(&f.socket).unwrap(), b"foreign socket replacement");
    fs::remove_file(&f.socket).unwrap();
    fs::rename(saved, &f.socket).unwrap();
    f.start();
    f.wait_empty();
    println!(
        "KF_FACTORY_FAULT_PROOF {}",
        serde_json::json!({"kind":"socket_replacement","foreign_preserved":true,"owned_restart_recovered":true})
    );
}
