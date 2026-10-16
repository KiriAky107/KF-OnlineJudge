use kf_sandbox::runtime::{RuntimeConfig, ValidatedRuntimeConfig};
use kf_sandbox::spec::{ExecutionSpec, SpecPolicy, ValidatedSpec};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static ATTEMPT: AtomicU64 = AtomicU64::new(0);

pub fn require_vm() {
    assert_eq!(
        std::env::var("KF_SANDBOX_INDEPENDENT_VM").as_deref(),
        Ok("1")
    );
    assert_eq!(
        fs::read_to_string("/kf/INDEPENDENT_VM").unwrap().trim(),
        "isolated-test-guest"
    );
    assert!(
        fs::read_to_string("/sys/class/dmi/id/sys_vendor")
            .unwrap()
            .contains("QEMU")
    );
    for path in ["/mnt/hgfs", "/home/kronecker", "/root/.ssh"] {
        assert!(!Path::new(path).exists());
    }
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    for forbidden in ["hgfs", "9p", "virtiofs"] {
        assert!(!mounts.contains(forbidden));
    }
    let net: Vec<_> = fs::read_dir("/sys/class/net")
        .unwrap()
        .map(|entry| entry.unwrap().file_name())
        .collect();
    assert_eq!(net, ["lo"]);
}

pub struct Root {
    pub path: PathBuf,
    pub config: RuntimeConfig,
}

impl Root {
    pub fn new() -> Self {
        require_vm();
        let path = PathBuf::from(format!(
            "/sys/fs/cgroup/kf-vm-{}-{}",
            std::process::id(),
            ATTEMPT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        let mut config: RuntimeConfig = serde_json::from_str(include_str!(
            "../../../../../deploy/sandbox.runtime.example.json"
        ))
        .unwrap();
        config.delegated_cgroup_root = path.to_str().unwrap().into();
        config.aggregate.task_cpu_quota_us = 10_000;
        Self { path, config }
    }
    pub fn validated(&self) -> ValidatedRuntimeConfig {
        self.config.clone().validate().unwrap()
    }
}

impl Drop for Root {
    fn drop(&mut self) {
        if self.path.exists() {
            let _ = fs::write(self.path.join("cgroup.kill"), "1");
            if let Ok(entries) = fs::read_dir(&self.path) {
                for entry in entries.flatten() {
                    if entry.file_type().is_ok_and(|kind| kind.is_dir()) {
                        let _ = fs::remove_dir(entry.path());
                    }
                }
            }
            let _ = fs::remove_dir(&self.path);
        }
    }
}

pub fn spec(id: &str, memory: u64, processes: u32) -> ValidatedSpec {
    let mut spec: ExecutionSpec =
        serde_json::from_str(include_str!("../fixtures/spec.valid.json")).unwrap();
    spec.run_id = id.into();
    spec.limits.memory_bytes = memory;
    spec.limits.processes = processes;
    spec.validate(&SpecPolicy::default()).unwrap()
}

pub fn binary() -> PathBuf {
    std::env::var_os("KF_SANDBOX_TEST_BIN")
        .expect("VM sandbox path")
        .into()
}
pub fn children() -> Vec<u32> {
    let mut children = Vec::new();
    for task in fs::read_dir("/proc/self/task").unwrap() {
        children.extend(
            fs::read_to_string(task.unwrap().path().join("children"))
                .unwrap()
                .split_whitespace()
                .map(|pid| pid.parse::<u32>().unwrap()),
        );
    }
    children.sort_unstable();
    children
}
