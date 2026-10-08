#![cfg(all(target_os = "linux", feature = "vm-acceptance"))]
#[allow(dead_code)]
mod support;
use kf_sandbox::spec::{ExecutionProfile, ExecutionSpec, InputArtifact, ResourceLimits};
use std::{
    fs,
    os::unix::{
        fs::{PermissionsExt, symlink},
        process::CommandExt,
    },
    path::PathBuf,
    process::Command,
};
struct Fixture {
    root: support::Root,
    dir: PathBuf,
    config: PathBuf,
}
impl Fixture {
    fn new() -> Self {
        support::require_vm();
        let mut root = support::Root::new();
        root.config.aggregate.task_cpu_quota_us = 100_000;
        root.config.per_task_ceiling.cpu_time_us = 30_000_000;
        root.config.per_task_ceiling.wall_time_us = 60_000_000;
        let dir = PathBuf::from(format!("/kf/operator-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        for child in ["inputs", "outputs"] {
            fs::create_dir(dir.join(child)).unwrap();
        }
        let config = dir.join("runtime.json");
        fs::write(&config, serde_json::to_vec(&root.config).unwrap()).unwrap();
        fs::set_permissions(&config, fs::Permissions::from_mode(0o600)).unwrap();
        Self { root, dir, config }
    }
    fn request(&self, id: &str, program: &str) -> ExecutionSpec {
        fs::copy(program, self.dir.join("inputs/program-001")).unwrap();
        fs::set_permissions(
            self.dir.join("inputs/program-001"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        fs::write(self.dir.join("inputs/input-001"), vec![0xa5; 65537]).unwrap();
        let mut s: ExecutionSpec =
            serde_json::from_str(include_str!("fixtures/spec.valid.json")).unwrap();
        s.run_id = id.into();
        s.argv.push("normal".into());
        s
    }
    fn command(&self, s: &ExecutionSpec) -> Command {
        fs::write(self.dir.join("spec.json"), serde_json::to_vec(s).unwrap()).unwrap();
        let mut c = Command::new(std::env::var_os("KF_SANDBOX_PRODUCTION_BIN").unwrap());
        c.env_clear()
            .args(["run", "--config"])
            .arg(&self.config)
            .arg("--spec")
            .arg(self.dir.join("spec.json"))
            .arg("--artifacts")
            .arg(self.dir.join("inputs"))
            .arg("--output")
            .arg(self.dir.join("outputs"));
        c
    }
    fn execute(&self, s: &ExecutionSpec) -> serde_json::Value {
        let o = self.command(s).output().unwrap();
        assert!(
            o.status.success(),
            "{} {}",
            String::from_utf8_lossy(&o.stdout),
            String::from_utf8_lossy(&o.stderr)
        );
        let v: serde_json::Value = serde_json::from_slice(&o.stdout).unwrap();
        assert_eq!(v["report"]["cleanup"], "complete");
        assert_eq!(v["report"]["errors"], serde_json::json!([]));
        assert_eq!(
            v["proof"]["order"],
            serde_json::json!([
                "verified_isolation",
                "exec_gate",
                "cgroup_empty",
                "owned_reaped",
                "final_accounting",
                "final_export",
                "resource_removal"
            ])
        );
        self.empty();
        println!("KF_PRODUCTION_RUN_PROOF {v}");
        v
    }
    fn empty(&self) {
        for name in ["tasks", "captures", "identities"] {
            let p = format!("/var/lib/kf-judge/{name}");
            if std::path::Path::new(&p).exists() {
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
    }
    fn refusal(&self, s: &ExecutionSpec, code: &str) {
        let o = self.command(s).output().unwrap();
        assert_eq!(
            o.status.code(),
            Some(2),
            "{}",
            String::from_utf8_lossy(&o.stderr)
        );
        assert!(o.stdout.is_empty());
        let v: serde_json::Value = serde_json::from_slice(&o.stderr).unwrap();
        assert_eq!(v["error"], code, "{v}");
        self.empty();
        println!("KF_PRODUCTION_REFUSAL_PROOF {v}");
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.dir);
    }
}
#[test]
#[ignore = "independent capped VM: default-build ordinary CLI, actual C/C++ compilation and sealed export"]
fn ordinary_default_binary_executes_native_and_compiler_profiles() {
    let f = Fixture::new();
    for (n, p) in [
        ("static", "/kf/bin/normal-static"),
        ("c", "/kf/bin/normal-c"),
        ("cpp", "/kf/bin/normal-cpp"),
    ] {
        let s = f.request(&format!("production-{n}"), p);
        assert_eq!(
            f.execute(&s)["outcome"],
            serde_json::json!({"kind":"exited","code":0})
        );
        assert!(
            !fs::read(f.dir.join(format!("outputs/{}/stdout", s.run_id)))
                .unwrap()
                .is_empty()
        );
    }
    for (tag, profile, driver) in [
        ("c", ExecutionProfile::CompileC, "/usr/bin/gcc"),
        ("cpp", ExecutionProfile::CompileCpp, "/usr/bin/g++"),
    ] {
        let mut s = f.request(&format!("production-compile-{tag}"), "/kf/bin/normal-c");
        fs::write(
            f.dir.join("inputs/source"),
            b"#include <stdio.h>\nint main(void){puts(\"compiled\");return 0;}\n",
        )
        .unwrap();
        s.profile = profile;
        s.argv = vec![
            driver.into(),
            "-O0".into(),
            format!("/work/main.{tag}"),
            "-o".into(),
            "/work/main.bin".into(),
        ];
        s.inputs = vec![InputArtifact {
            artifact_id: "source".into(),
            path: format!("main.{tag}"),
        }];
        s.stdin_artifact = None;
        s.outputs = vec!["main.bin".into()];
        s.limits = ResourceLimits::for_profile(profile);
        s.limits.cpu_time_us = 30_000_000;
        s.limits.wall_time_us = 60_000_000;
        f.execute(&s);
        let elf = f.dir.join(format!("outputs/{}/artifact-0", s.run_id));
        assert!(fs::read(&elf).unwrap().starts_with(b"\x7fELF"));
        let run = f.request(&format!("production-compiled-{tag}"), elf.to_str().unwrap());
        f.execute(&run);
        assert_eq!(
            fs::read(f.dir.join(format!("outputs/{}/stdout", run.run_id))).unwrap(),
            b"compiled\n"
        );
    }
}
#[test]
#[ignore = "independent capped VM: real unsupported helper identity/config/image/scope and invalid request refusal"]
fn ordinary_entry_refuses_untrusted_and_unsupported_runtime() {
    let f = Fixture::new();
    let mut s = f.request("production-refused", "/kf/bin/normal-c");
    let o = f.command(&s).uid(1000).gid(1000).output().unwrap();
    assert_eq!(o.status.code(), Some(2));
    assert!(o.stdout.is_empty());
    f.empty();
    fs::set_permissions(&f.config, fs::Permissions::from_mode(0o666)).unwrap();
    f.refusal(&s, "untrusted_runtime_config");
    fs::set_permissions(&f.config, fs::Permissions::from_mode(0o600)).unwrap();
    let held = f.dir.join("runtime-held.json");
    fs::rename(&f.config, &held).unwrap();
    symlink(&held, &f.config).unwrap();
    f.refusal(&s, "untrusted_runtime_config");
    fs::remove_file(&f.config).unwrap();
    fs::rename(held, &f.config).unwrap();
    let image = "/var/lib/kf-judge/images/gnu-cxx-x86_64-v1/manifest.json";
    fs::rename(image, format!("{image}.held")).unwrap();
    f.refusal(&s, "runtime_not_ready");
    fs::rename(format!("{image}.held"), image).unwrap();
    let c = fs::read_to_string("/proc/self/cgroup").unwrap();
    let scope = format!(
        "/sys/fs/cgroup{}/cpu.max",
        c.trim().strip_prefix("0::").unwrap()
    );
    let before = fs::read_to_string(&scope).unwrap();
    fs::write(&scope, "max 100000").unwrap();
    f.refusal(&s, "runtime_not_ready");
    fs::write(scope, before).unwrap();
    s.inputs[0].path = "../escape".into();
    f.refusal(&s, "invalid_execution_spec");
    println!(
        "KF_PRODUCTION_AUTHORITY_PROOF ordinary_worker_refused=1 writable_config_refused=1 symlink_config_refused=1 missing_image_manifest_refused=1 unlimited_helper_scope_refused=1 invalid_request_refused=1"
    );
}
#[test]
#[ignore = "independent capped VM: actual default-path seccomp and failed atomic publication preserve previous bytes"]
fn default_isolation_and_no_overwrite_publication() {
    let f = Fixture::new();
    let mut s = f.request("production-danger", "/kf/bin/isolation-probe");
    s.argv = vec!["/work/main".into(), "danger".into()];
    s.stdin_artifact = None;
    assert_eq!(
        f.execute(&s)["outcome"],
        serde_json::json!({"kind":"exited","code":0})
    );
    let s = f.request("production-preserve", "/kf/bin/normal-c");
    let prior = f.dir.join("outputs/production-preserve");
    fs::create_dir(&prior).unwrap();
    fs::write(prior.join("foreign"), b"keep prior publication").unwrap();
    f.refusal(&s, "output_publication_failed");
    assert_eq!(
        fs::read(prior.join("foreign")).unwrap(),
        b"keep prior publication"
    );
    assert!(!f.dir.join("outputs/.production-preserve.pending").exists());
    println!(
        "KF_PRODUCTION_PUBLICATION_PROOF default_native_danger_policy=1 existing_output_untouched=1 pending_cleanup_complete=1"
    );
}
