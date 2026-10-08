#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use std::{
    fs,
    io::Write,
    os::unix::fs::PermissionsExt,
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

fn command(case: &str) -> Command {
    let mut c = Command::new(support::binary());
    c.args(["vm-case", case])
        .env_clear()
        .env("KF_SANDBOX_INDEPENDENT_VM", "1");
    c
}
fn parsed(output: std::process::Output) -> serde_json::Value {
    assert!(
        output.status.success(),
        "{} {}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let v: serde_json::Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        v["outcome"],
        serde_json::json!({"kind":"exited","code":0}),
        "{v}"
    );
    assert_eq!(v["report"]["cleanup"], "complete");
    assert_eq!(v["report"]["errors"], serde_json::json!([]));
    assert_eq!(v["report"]["limits"], serde_json::json!([]));
    println!("KF_ISOLATION_MATRIX_PROOF {v}");
    v
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
#[test]
#[ignore = "independent capped VM: real compiler wrappers, dangerous syscalls and actual setuid image"]
fn all_profiles_keep_network_namespaces_process_and_privilege_boundaries() {
    support::require_vm();
    for profile in ["native", "c", "cpp"] {
        let v = parsed(
            command(&format!("isolation-danger-{profile}"))
                .output()
                .unwrap(),
        );
        let s = v["probe_stdout"].as_str().unwrap();
        assert!(
            s.contains("denied=49")
                && s.contains("network=1 ptrace=1 namespaces=1 privilege=1 signal=1 device=1"),
            "{s}"
        );
        empty();
        let v = parsed(
            command(&format!("isolation-identity-{profile}"))
                .output()
                .unwrap(),
        );
        assert_eq!(v["probe_stdout"], "IDENTITY_OK uid=100000 euid=100000\n");
        empty();
    }
}
struct Peer {
    child: Option<Child>,
    work: String,
}
impl Drop for Peer {
    fn drop(&mut self) {
        if let Some(child) = self.child.as_mut() {
            if !self.work.is_empty() {
                let _ = fs::write(format!("{}/peer-stop", self.work), b"stop");
            }
            let deadline = Instant::now() + Duration::from_secs(10);
            while child.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            if child.try_wait().ok().flatten().is_none() {
                let _ = child.kill();
            }
            let _ = child.wait();
        }
        let _ = fs::remove_file("/kf/isolation-peer-frame");
    }
}
#[test]
#[ignore = "independent capped VM: simultaneously live production tasks with distinct leased UID and six namespaces"]
fn live_peer_files_and_processes_remain_inaccessible_to_every_profile() {
    support::require_vm();
    let child = command("isolation-peer")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let owner = child.id();
    let mut peer = Peer {
        child: Some(child),
        work: String::new(),
    };
    let cg = format!("/sys/fs/cgroup/kf-acceptance-{owner}/kf-isolation-peer");
    let deadline = Instant::now() + Duration::from_secs(8);
    let (pid, uid, init) = loop {
        assert!(
            peer.child.as_mut().unwrap().try_wait().unwrap().is_none(),
            "peer exited before readiness"
        );
        assert!(Instant::now() < deadline, "peer readiness deadline");
        let members = fs::read_to_string(format!("{cg}/cgroup.procs")).unwrap_or_default();
        let mut found = None;
        let mut init = 0;
        for id in members.split_whitespace() {
            let Ok(status) = fs::read_to_string(format!("/proc/{id}/status")) else {
                continue;
            };
            let ns = status
                .lines()
                .find_map(|l| l.strip_prefix("NSpid:"))
                .unwrap_or("")
                .split_whitespace()
                .last();
            if ns == Some("1") {
                init = id.parse::<u32>().unwrap();
            }
            if ns == Some("2")
                && fs::read(format!("/proc/{id}/root/work/peer-ready")).is_ok_and(|s| s == b"ready")
            {
                let uid = status
                    .lines()
                    .find_map(|l| l.strip_prefix("Uid:"))
                    .unwrap()
                    .split_whitespace()
                    .next()
                    .unwrap()
                    .parse::<u32>()
                    .unwrap();
                found = Some((id.parse::<u32>().unwrap(), uid));
            }
        }
        if let Some((pid, uid)) = found
            && init > 1
        {
            break (pid, uid, init);
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    assert_eq!(uid, 100000);
    peer.work = format!("/proc/{pid}/root/work");
    let pidfd = rustix::process::pidfd_open(
        rustix::process::Pid::from_raw(pid as i32).unwrap(),
        rustix::process::PidfdFlags::empty(),
    )
    .unwrap();
    assert!(
        rustix::io::fcntl_getfd(&pidfd)
            .unwrap()
            .contains(rustix::io::FdFlags::CLOEXEC)
    );
    let names = ["mnt", "pid", "net", "ipc", "uts", "cgroup"];
    let mut ids = Vec::new();
    for name in names {
        let target = fs::read_link(format!("/proc/{pid}/ns/{name}")).unwrap();
        let s = target.to_str().unwrap();
        ids.push(
            s.split_once('[')
                .unwrap()
                .1
                .trim_end_matches(']')
                .parse::<u64>()
                .unwrap(),
        );
    }
    let frame = format!(
        "{pid} {init} {owner} {uid} {}\n",
        ids.iter().map(u64::to_string).collect::<Vec<_>>().join(" ")
    );
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open("/kf/isolation-peer-frame")
        .unwrap();
    file.set_permissions(fs::Permissions::from_mode(0o600))
        .unwrap();
    file.write_all(frame.as_bytes()).unwrap();
    drop(file);
    for profile in ["native", "c", "cpp"] {
        let v = parsed(
            command(&format!("isolation-cross-{profile}"))
                .output()
                .unwrap(),
        );
        assert_eq!(
            v["probe_stdout"],
            "CROSS_OK peer=100000 self=100001 denied=8\n"
        );
        assert!(peer.child.as_mut().unwrap().try_wait().unwrap().is_none());
        assert_eq!(
            fs::read(format!("{}/peer-secret", peer.work)).unwrap(),
            b"peer-private-content"
        );
        assert!(
            fs::read_to_string(format!("{cg}/cgroup.events"))
                .unwrap()
                .contains("populated 1")
        );
        assert_eq!(
            fs::read_dir("/var/lib/kf-judge/identities")
                .unwrap()
                .count(),
            1
        );
    }
    println!(
        "KF_LIVE_PEER_KERNEL_PROOF {}",
        serde_json::json!({"owner_pid":owner,"main_pid":pid,"init_pid":init,"uid":uid,"namespaces":ids,"three_attacker_profiles_uid":100001,"secret_unchanged_after_each_attack":true,"peer_cgroup_still_populated":true})
    );
    fs::write(format!("{}/peer-stop", peer.work), b"stop").unwrap();
    let deadline = Instant::now() + Duration::from_secs(10);
    while peer.child.as_mut().unwrap().try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(5));
    }
    let v = parsed(peer.child.take().unwrap().wait_with_output().unwrap());
    assert_eq!(v["probe_stdout"], "PEER_OK uid=100000\n");
    drop(pidfd);
    drop(peer);
    empty();
}
