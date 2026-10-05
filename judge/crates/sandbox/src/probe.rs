use serde::Serialize;
use std::fs;

#[derive(Debug, Serialize)]
pub struct EnvironmentObservation {
    pub os: &'static str,
    pub namespace_entries: Vec<String>,
    pub cgroup_v2_visible: bool,
    pub cgroup_v2_controllers: Vec<String>,
    pub current_process_seccomp_mode: Option<u32>,
    pub isolation_verified: bool,
}

/// Filesystem observations do not verify delegation, syscall access or safe execution.
pub fn observe() -> EnvironmentObservation {
    let mut namespace_entries: Vec<String> = fs::read_dir("/proc/self/ns")
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .map(|entry| entry.file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default();
    namespace_entries.sort();
    let controllers = fs::read_to_string("/sys/fs/cgroup/cgroup.controllers");
    let cgroup_v2_visible = controllers.is_ok();
    let cgroup_v2_controllers = controllers
        .unwrap_or_default()
        .split_whitespace()
        .map(str::to_owned)
        .collect();
    let current_process_seccomp_mode =
        fs::read_to_string("/proc/self/status")
            .ok()
            .and_then(|status| {
                status.lines().find_map(|line| {
                    line.strip_prefix("Seccomp:")
                        .and_then(|mode| mode.trim().parse().ok())
                })
            });
    EnvironmentObservation {
        os: std::env::consts::OS,
        namespace_entries,
        cgroup_v2_visible,
        cgroup_v2_controllers,
        current_process_seccomp_mode,
        isolation_verified: false,
    }
}
