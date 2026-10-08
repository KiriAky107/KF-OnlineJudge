#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use rustix::{
    fd::{AsFd, OwnedFd},
    process::{WaitId, WaitIdOptions},
};
use std::{
    fs,
    os::unix::{
        fs::{PermissionsExt, symlink},
        process::ExitStatusExt,
    },
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};
const LEASE: &str = "/var/lib/kf-judge/identities/uid-100000/owner.json";
fn command(case: &str) -> Command {
    let mut c = Command::new(support::binary());
    c.args(["vm-case", case])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1");
    c
}
fn recover() -> serde_json::Value {
    let o = command("restart-recover").output().unwrap();
    assert!(
        o.status.success(),
        "{} {}",
        String::from_utf8_lossy(&o.stdout),
        String::from_utf8_lossy(&o.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&o.stdout).unwrap();
    println!("KF_RESTART_RECOVERY_PROOF {v}");
    v
}
fn record() -> serde_json::Value {
    serde_json::from_slice(&fs::read(LEASE).unwrap()).unwrap()
}
fn store(v: &serde_json::Value) {
    fs::write(LEASE, serde_json::to_vec(v).unwrap()).unwrap();
}
fn empty() {
    for dir in ["tasks", "identities", "captures"] {
        assert_eq!(
            fs::read_dir(format!("/var/lib/kf-judge/{dir}"))
                .unwrap()
                .count(),
            0
        );
    }
}
struct Owner {
    child: Option<Child>,
    cg: String,
    publications: String,
    run_id: String,
    members: Vec<OwnedFd>,
    previous: Option<rustix::process::Pid>,
    residual: u32,
}
impl Owner {
    fn start(stage: &str) -> Self {
        support::require_vm();
        assert!(support::children().is_empty());
        let previous = rustix::process::child_subreaper().unwrap();
        rustix::process::set_child_subreaper(rustix::process::Pid::from_raw(1)).unwrap();
        let _ = fs::remove_file("/kf/restart-ready");
        let mut child = command(&format!("restart-{stage}"))
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let owner = child.id();
        let cg = format!("/sys/fs/cgroup/kf-acceptance-{owner}");
        let deadline = Instant::now() + Duration::from_secs(8);
        let ready: serde_json::Value = loop {
            assert!(
                child.try_wait().unwrap().is_none(),
                "supervisor exited before ready"
            );
            assert!(
                Instant::now() < deadline,
                "crash fixture readiness deadline"
            );
            if let Ok(bytes) = fs::read("/kf/restart-ready") {
                break serde_json::from_slice(&bytes).unwrap();
            }
            std::thread::sleep(Duration::from_millis(5));
        };
        assert_eq!(ready["owner_pid"], owner);
        assert_eq!(ready["stage"], stage);
        let run_id = ready["run_id"].as_str().unwrap().to_owned();
        if stage == "live" {
            loop {
                let pids = fs::read_to_string(format!("{cg}/kf-{run_id}/cgroup.procs")).unwrap();
                if pids.split_whitespace().any(|p| {
                    fs::read(format!("/proc/{p}/root/work/peer-ready")).is_ok_and(|s| s == b"ready")
                }) {
                    break;
                }
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(5));
            }
        }
        let members = fs::read_to_string(format!("{cg}/kf-{run_id}/cgroup.procs"))
            .unwrap()
            .split_whitespace()
            .map(|n| {
                rustix::process::pidfd_open(
                    rustix::process::Pid::from_raw(n.parse().unwrap()).unwrap(),
                    rustix::process::PidfdFlags::empty(),
                )
                .unwrap()
            })
            .collect();
        let r = record();
        assert_eq!(r["owner"]["pid"], owner);
        assert_eq!(r["version"], 2);
        assert_eq!(r["service"], "kf-sandbox-task-v2");
        for key in ["task", "cgroup", "capture"] {
            assert!(r["resources"][key]["ino"].as_u64().unwrap() > 0);
        }
        let mut cfg: kf_sandbox::runtime::RuntimeConfig = serde_json::from_str(include_str!(
            "../../../../deploy/sandbox.runtime.example.json"
        ))
        .unwrap();
        cfg.delegated_cgroup_root = cg.clone();
        fs::write(
            "/kf/restart-runtime.json",
            serde_json::to_vec(&cfg).unwrap(),
        )
        .unwrap();
        fs::set_permissions(
            "/kf/restart-runtime.json",
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        Self {
            child: Some(child),
            cg,
            publications: format!("/kf/execution-publications-{owner}"),
            run_id,
            members,
            previous,
            residual: ready["residual_pid"].as_u64().unwrap() as u32,
        }
    }
    fn crash(&mut self) {
        self.child.as_mut().unwrap().kill().unwrap();
        assert_eq!(self.child.take().unwrap().wait().unwrap().signal(), Some(9));
        assert!(
            fs::read_to_string(format!("/proc/{}/stat", self.residual)).is_ok(),
            "real partial-preparation residual must survive owner SIGKILL"
        );
    }
    fn group(&self) -> String {
        format!("{}/kf-{}", self.cg, self.run_id)
    }
    fn task(&self) -> String {
        format!("/var/lib/kf-judge/tasks/kf-{}", self.run_id)
    }
    fn reap(&self) {
        let deadline = Instant::now() + Duration::from_secs(3);
        for fd in &self.members {
            loop {
                match rustix::process::waitid(
                    WaitId::PidFd(fd.as_fd()),
                    WaitIdOptions::EXITED | WaitIdOptions::NOHANG,
                ) {
                    Ok(Some(_)) | Err(rustix::io::Errno::CHILD) => break,
                    Ok(None) => {
                        assert!(Instant::now() < deadline);
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(e) => panic!("owned pidfd wait: {e}"),
                }
            }
            let mut p = [rustix::event::PollFd::new(fd, rustix::event::PollFlags::IN)];
            rustix::event::poll(
                &mut p,
                Some(&rustix::event::Timespec {
                    tv_sec: 0,
                    tv_nsec: 0,
                }),
            )
            .unwrap();
            assert!(
                p[0].revents()
                    .intersects(rustix::event::PollFlags::IN | rustix::event::PollFlags::HUP)
            );
        }
        assert!(
            support::children().is_empty(),
            "all owned adopted children reaped"
        );
    }
    fn finish(&self) {
        self.reap();
        assert!(!std::path::Path::new(&self.group()).exists());
        empty();
        assert_eq!(recover()["items"], serde_json::json!([]));
        println!(
            "KF_RESTART_KERNEL_PROOF {}",
            serde_json::json!({"run_id":self.run_id,"residual_pid":self.residual,"owner_killed_sigkill":true,"kernel_pidfd_exits_verified":self.members.len(),"only_owned_adopted_children_reaped":true,"resources_removed":true,"repeat_cleanup_empty":true})
        );
    }
}
impl Drop for Owner {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            let _ = child.kill();
            let _ = child.wait();
        }
        let _ = fs::write(format!("{}/cgroup.kill", self.cg), "1");
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            match rustix::process::wait(rustix::process::WaitOptions::NOHANG) {
                Ok(Some(_)) => {}
                Err(rustix::io::Errno::CHILD) => break,
                _ => std::thread::sleep(Duration::from_millis(5)),
            }
        }
        let _ = fs::remove_dir(self.group());
        let _ = fs::remove_dir(&self.cg);
        // Fixture input publications belong to the trusted caller, not to
        // sandbox recovery. Remove only this known test-owned publication.
        let _ = fs::remove_dir_all(&self.publications);
        let _ = fs::remove_file("/kf/restart-ready");
        let _ = fs::remove_file("/kf/restart-runtime.json");
        let _ = rustix::process::set_child_subreaper(self.previous);
    }
}
#[test]
#[ignore = "independent capped VM: real SIGKILL and service restart before/after user exec with surviving residual"]
fn actual_owner_death_reclaims_prepared_and_live_tasks() {
    for stage in ["prepared", "live"] {
        let mut owner = Owner::start(stage);
        assert_eq!(recover()["items"][0]["status"], "active");
        assert!(owner.child.as_mut().unwrap().try_wait().unwrap().is_none());
        owner.crash();
        let v = recover();
        assert_eq!(v["items"][0]["status"], "reclaimed", "{v}");
        assert!(v["items"][0]["stopped_processes"].as_u64().unwrap() > 0);
        owner.finish();
        drop(owner);
        empty();
    }
}
#[test]
#[ignore = "independent capped VM: failed resource deletion must preserve separate error, UID and foreign bytes"]
fn cleanup_failure_is_independent_and_reserved_until_explicit_repair() {
    let mut owner = Owner::start("live");
    fs::write(
        format!("{}/foreign", owner.task()),
        b"preserve this foreign content",
    )
    .unwrap();
    owner.crash();
    let v = recover();
    assert_eq!(v["items"][0]["status"], "cleanup_failed");
    assert_eq!(v["items"][0]["cleanup_error"]["stage"], "cleanup");
    assert_eq!(v["items"][0]["cleanup_error"]["code"], "cleanup_incomplete");
    assert_eq!(
        fs::read(format!("{}/foreign", owner.task())).unwrap(),
        b"preserve this foreign content"
    );
    assert!(std::path::Path::new(LEASE).exists());
    assert!(!std::path::Path::new(&owner.group()).exists());
    owner.reap();
    assert_eq!(recover()["items"][0]["status"], "cleanup_failed");
    fs::remove_file(format!("{}/foreign", owner.task())).unwrap();
    assert_eq!(recover()["items"][0]["status"], "reclaimed");
    owner.finish();
}
#[test]
#[ignore = "independent capped VM: replaced cgroup inode, symlink, boot/PID identity and unknown markers stay protected"]
fn replaced_resources_and_unrecognized_markers_are_not_adopted() {
    let mut owner = Owner::start("prepared");
    owner.crash();
    let saved = record();
    // A reused owner PID/start pair is live even without a surviving flock.
    let stat = fs::read_to_string("/proc/self/stat").unwrap();
    let ticks = stat
        .rsplit_once(") ")
        .unwrap()
        .1
        .split_whitespace()
        .nth(19)
        .unwrap()
        .parse::<u64>()
        .unwrap();
    let mut v = saved.clone();
    v["owner"]["pid"] = std::process::id().into();
    v["owner"]["start_ticks"] = ticks.into();
    store(&v);
    assert_eq!(recover()["items"][0]["status"], "active");
    store(&saved);
    v = saved.clone();
    v["owner"]["boot_id"] = "00000000-0000-0000-0000-000000000000".into();
    store(&v);
    assert_eq!(recover()["items"][0]["status"], "cleanup_failed");
    store(&saved);
    // Startup must inspect a foreign FIFO without reading or blocking on it.
    let held_record = format!("{LEASE}.held");
    fs::rename(LEASE, &held_record).unwrap();
    rustix::fs::mknodat(
        rustix::fs::CWD,
        LEASE,
        rustix::fs::FileType::Fifo,
        rustix::fs::Mode::from_raw_mode(0o600),
        0,
    )
    .unwrap();
    let before = Instant::now();
    assert_eq!(recover()["items"][0]["status"], "unrecognized");
    assert!(before.elapsed() < Duration::from_secs(2));
    fs::remove_file(LEASE).unwrap();
    fs::rename(held_record, LEASE).unwrap();
    v = saved.clone();
    v["resources"]["cgroup"] = serde_json::Value::Null;
    store(&v);
    assert_eq!(recover()["items"][0]["status"], "cleanup_failed");
    store(&saved);
    // cgroup v2 deliberately forbids rename. The VM operator first stops and
    // removes its known original fixture, then creates a genuinely different
    // cgroup at the same name while retaining the old recorded inode.
    fs::write(format!("{}/cgroup.kill", owner.group()), "1").unwrap();
    owner.reap();
    fs::remove_dir(owner.group()).unwrap();
    fs::create_dir(owner.group()).unwrap();
    fs::set_permissions(owner.group(), fs::Permissions::from_mode(0o700)).unwrap();
    let mut foreign = command("restart-residual")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    fs::write(
        format!("{}/cgroup.procs", owner.group()),
        foreign.id().to_string(),
    )
    .unwrap();
    assert_eq!(recover()["items"][0]["status"], "cleanup_failed");
    assert!(foreign.try_wait().unwrap().is_none());
    foreign.kill().unwrap();
    foreign.wait().unwrap();
    fs::remove_dir(owner.group()).unwrap();
    let old_task = format!("{}-held", owner.task());
    fs::rename(owner.task(), &old_task).unwrap();
    fs::create_dir("/kf/protected-restart-directory").unwrap();
    fs::write(
        "/kf/protected-restart-directory/foreign",
        b"do not traverse",
    )
    .unwrap();
    symlink("/kf/protected-restart-directory", owner.task()).unwrap();
    assert_eq!(recover()["items"][0]["status"], "cleanup_failed");
    assert_eq!(
        fs::read("/kf/protected-restart-directory/foreign").unwrap(),
        b"do not traverse"
    );
    fs::remove_file(owner.task()).unwrap();
    fs::rename(old_task, owner.task()).unwrap();
    fs::remove_file("/kf/protected-restart-directory/foreign").unwrap();
    fs::remove_dir("/kf/protected-restart-directory").unwrap();
    let unknown = "/var/lib/kf-judge/identities/uid-100001";
    fs::create_dir(unknown).unwrap();
    fs::set_permissions(unknown, fs::Permissions::from_mode(0o700)).unwrap();
    let legacy = b"{\"version\":1,\"run_id\":\"foreign\",\"uid\":100001,\"gid\":100001}";
    fs::write(format!("{unknown}/owner.json"), legacy).unwrap();
    fs::set_permissions(
        format!("{unknown}/owner.json"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let v = recover();
    assert_eq!(v["items"][0]["status"], "reclaimed");
    assert_eq!(v["items"][1]["status"], "unrecognized");
    assert_eq!(fs::read(format!("{unknown}/owner.json")).unwrap(), legacy);
    fs::remove_file(format!("{unknown}/owner.json")).unwrap();
    fs::remove_dir(unknown).unwrap();
    owner.finish();
    println!(
        "KF_RESTART_OWNERSHIP_PROOF live_owner_pid_start=1 boot_mismatch_refused=1 unrecorded_resource_refused=1 fifo_not_opened=1 replaced_cgroup_and_process_untouched=1 symlink_not_followed=1 legacy_marker_untouched=1"
    );
}
