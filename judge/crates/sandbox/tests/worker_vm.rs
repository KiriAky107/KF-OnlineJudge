#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use std::{
    fs,
    os::unix::{
        fs::{PermissionsExt, chown},
        net::UnixStream,
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
struct Fixture {
    root: support::Root,
    dir: PathBuf,
    config: PathBuf,
    socket: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        support::require_vm();
        let mut root = support::Root::new();
        root.config.aggregate.task_cpu_quota_us = 100_000;
        let dir = PathBuf::from(format!("/kf/worker-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o711)).unwrap();
        let config = dir.join("runtime.json");
        fs::write(&config, serde_json::to_vec(&root.config).unwrap()).unwrap();
        fs::set_permissions(&config, fs::Permissions::from_mode(0o600)).unwrap();
        let p = dir.join("inputs");
        fs::create_dir(&p).unwrap();
        chown(&p, Some(1000), Some(1000)).unwrap();
        fs::set_permissions(&p, fs::Permissions::from_mode(0o700)).unwrap();
        fs::copy("/kf/bin/worker-native", p.join("program")).unwrap();
        Self::worker_file(&p.join("program"));
        let socket = dir.join("socket");
        Self {
            root,
            dir,
            config,
            socket,
        }
    }
    fn worker_file(p: &Path) {
        chown(p, Some(1000), Some(1000)).unwrap();
        fs::set_permissions(p, fs::Permissions::from_mode(0o600)).unwrap();
    }
    fn job(&self, id: &str, input: &[u8], expected: &[u8]) {
        for (name, data) in [("stdin", input), ("expected", expected)] {
            let p = self.dir.join(format!("inputs/{name}"));
            fs::write(&p, data).unwrap();
            Self::worker_file(&p);
        }
        let j = serde_json::json!({"request":{"run_id":id,"program_artifact":"program","stdin_artifact":"stdin"},"expected_artifact":"expected"});
        fs::write(self.dir.join("job.json"), serde_json::to_vec(&j).unwrap()).unwrap();
        Self::worker_file(&self.dir.join("job.json"));
    }
    fn helper(&self) -> Child {
        let mut c = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
            .env_clear()
            .args(["serve-one", "--config"])
            .arg(&self.config)
            .arg("--socket")
            .arg(&self.socket)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !self.socket.exists() {
            assert!(
                c.try_wait().unwrap().is_none(),
                "helper exited before socket"
            );
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        // Socket creation precedes mode installation; wait for actual published permissions.
        while fs::metadata(&self.socket).unwrap().permissions().mode() & 0o777 != 0o660 {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(2));
        }
        c
    }
    fn empty(&self) {
        for p in ["tasks", "identities", "captures"] {
            let p = PathBuf::from(format!("/var/lib/kf-judge/{p}"));
            if p.exists() {
                assert_eq!(fs::read_dir(p).unwrap().count(), 0);
            }
        }
        assert!(
            !fs::read_dir(&self.root.path).unwrap().any(|e| e
                .unwrap()
                .file_type()
                .unwrap()
                .is_dir())
        );
        assert!(!self.socket.exists());
    }
    fn execute(&self, id: &str, input: &[u8], expected: &[u8], verdict: &str) {
        self.job(id, input, expected);
        let h = self.helper();
        let w = Command::new(std::env::var_os("KF_WORKER_TEST_BIN").unwrap())
            .env_clear()
            .args(["judge-once", "--socket"])
            .arg(&self.socket)
            .arg("--job")
            .arg(self.dir.join("job.json"))
            .arg("--artifacts")
            .arg(self.dir.join("inputs"))
            .uid(1000)
            .gid(1000)
            .output()
            .unwrap();
        assert!(w.status.success(), "{}", String::from_utf8_lossy(&w.stderr));
        let j: serde_json::Value = serde_json::from_slice(&w.stdout).unwrap();
        assert_eq!(j["verdict"], verdict, "{j}");
        assert_eq!(j["cases"][0]["run_id"], id);
        assert!(j["cases"][0]["cpu_time_us"].as_u64().unwrap() > 0);
        let o = h.wait_with_output().unwrap();
        assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
        let v: serde_json::Value = serde_json::from_slice(&o.stdout).unwrap();
        assert_eq!(v["report"]["cleanup"], "complete");
        assert_eq!(v["report"]["errors"], serde_json::json!([]));
        self.empty();
        println!(
            "KF_WORKER_CHAIN_PROOF {}",
            serde_json::json!({"worker_uid":1000,"worker_gid":1000,"kernel_peer_authentication":true,"judgement":j,"sandbox":v,"expected_artifact_not_mounted":true})
        );
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}
#[test]
#[ignore = "independent capped VM: actual ordinary Worker -> Engine -> default-build root helper"]
fn native_binary_comparison_and_execution_failures() {
    let f = Fixture::new();
    f.execute("worker-ac", &[0, 255, 10], &[0, 255, 10], "accepted");
    f.execute("worker-wa", &[0, 255, 10], &[0, 255], "wrong_answer");
    f.execute("worker-re", b"#", b"", "runtime_error");
    f.execute("worker-tle", b"!", b"live", "time_limit_exceeded");
}
#[test]
#[ignore = "independent capped VM: real kernel peer credential denial and foreign socket preservation"]
fn mismatched_peer_and_existing_socket_are_refused() {
    let f = Fixture::new();
    let h = f.helper();
    let c = UnixStream::connect(&f.socket).unwrap();
    drop(c);
    let o = h.wait_with_output().unwrap();
    assert_eq!(o.status.code(), Some(2));
    assert!(o.stdout.is_empty());
    f.empty();
    f.job("worker-owner", b"!", b"");
    let publication = f.dir.join("inputs");
    chown(&publication, Some(0), Some(0)).unwrap();
    fs::set_permissions(&publication, fs::Permissions::from_mode(0o555)).unwrap();
    let h = f.helper();
    let mut spec: kf_sandbox::spec::ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    spec.run_id = "worker-owner".into();
    spec.inputs[0].artifact_id = "program".into();
    spec.stdin_artifact = Some("stdin".into());
    let path = f.dir.join("wire.json");
    fs::write(
        &path,
        serde_json::to_vec(&serde_json::json!({"spec":spec})).unwrap(),
    )
    .unwrap();
    Fixture::worker_file(&path);
    let c = Command::new("/kf/bin/worker-disconnect")
        .arg(&f.socket)
        .arg(&publication)
        .arg(&path)
        .output()
        .unwrap();
    assert!(c.status.success());
    let o = h.wait_with_output().unwrap();
    assert_eq!(o.status.code(), Some(2));
    assert!(o.stdout.is_empty());
    f.empty();
    chown(&publication, Some(1000), Some(1000)).unwrap();
    fs::set_permissions(&publication, fs::Permissions::from_mode(0o700)).unwrap();
    fs::write(&f.socket, b"foreign socket name").unwrap();
    let o = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap())
        .args(["serve-one", "--config"])
        .arg(&f.config)
        .arg("--socket")
        .arg(&f.socket)
        .output()
        .unwrap();
    assert_eq!(o.status.code(), Some(2));
    assert_eq!(fs::read(&f.socket).unwrap(), b"foreign socket name");
    fs::remove_file(&f.socket).unwrap();
    f.empty();
    println!(
        "KF_WORKER_AUTH_PROOF wrong_root_peer_refused=1 root_owned_publication_refused=1 foreign_socket_preserved=1 zero_resources=1"
    );
}
#[test]
#[ignore = "independent capped VM: authenticated ordinary client disconnect while actual task is live"]
fn client_disconnect_cancels_entire_task_and_cleans() {
    let f = Fixture::new();
    f.job("worker-cancel", b"!", b"");
    let h = f.helper();
    let mut spec: kf_sandbox::spec::ExecutionSpec =
        serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
    spec.run_id = "worker-cancel".into();
    spec.inputs[0].artifact_id = "program".into();
    spec.stdin_artifact = Some("stdin".into());
    let path = f.dir.join("wire.json");
    fs::write(
        &path,
        serde_json::to_vec(&serde_json::json!({"spec":spec})).unwrap(),
    )
    .unwrap();
    Fixture::worker_file(&path);
    let c = Command::new("/kf/bin/worker-disconnect")
        .arg(&f.socket)
        .arg(f.dir.join("inputs"))
        .arg(&path)
        .output()
        .unwrap();
    assert!(c.status.success(), "{}", String::from_utf8_lossy(&c.stderr));
    let o = h.wait_with_output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let v: serde_json::Value = serde_json::from_slice(&o.stdout).unwrap();
    assert!(v["report"]["cancelled_at_wall_time_us"].as_u64().unwrap() > 0);
    assert_eq!(v["report"]["stdout"]["retained_bytes"], 4);
    assert_eq!(v["report"]["cleanup"], "complete");
    assert_eq!(v["report"]["errors"], serde_json::json!([]));
    f.empty();
    println!("KF_WORKER_DISCONNECT_PROOF {v}");
}
