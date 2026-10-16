#![cfg(target_os = "linux")]
#[allow(dead_code)]
mod support;
use kf_sandbox::{
    cgroup::DelegatedCgroup,
    launcher::blocked::BlockedTarget,
    outcome::{LimitKind, Measurement, ResourceUsage},
    spec::{SpecPolicy, ValidatedSpec},
    supervisor::{BudgetEvent, BudgetSupervisor},
};
use std::os::unix::process::ExitStatusExt;
use std::{
    fs,
    path::Path,
    time::{Duration, Instant},
};
fn value(m: &Measurement<u64>) -> u64 {
    let Measurement::Measured { value } = m else {
        panic!("unavailable actual meter")
    };
    *value
}
fn spec(id: &str, cpu: u64, wall: u64, memory: u64) -> ValidatedSpec {
    let mut s = support::spec(id, memory, 8).spec().clone();
    s.limits.cpu_time_us = cpu;
    s.limits.wall_time_us = wall;
    s.validate(&SpecPolicy::default()).unwrap()
}
fn resume(path: &Path, child: &BlockedTarget) {
    fs::write(path.join("cgroup.freeze"), "0").unwrap();
    let fd = rustix::process::pidfd_open(
        rustix::process::Pid::from_raw(child.pid() as i32).unwrap(),
        rustix::process::PidfdFlags::empty(),
    )
    .unwrap();
    rustix::process::pidfd_send_signal(&fd, rustix::process::Signal::CONT).unwrap();
}
fn orphans() -> u32 {
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut count = 0;
    loop {
        match rustix::process::wait(rustix::process::WaitOptions::NOHANG) {
            Ok(Some(_)) => count += 1,
            Err(rustix::io::Errno::CHILD) => return count,
            Ok(None) => {
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(2));
            }
            Err(e) => panic!("orphan wait: {e}"),
        }
    }
}
fn run(
    mode: &str,
) -> (
    ResourceUsage,
    Vec<kf_sandbox::outcome::LimitObservation>,
    i32,
    u32,
) {
    support::require_vm();
    rustix::process::set_child_subreaper(Some(rustix::process::getpid())).unwrap();
    let root = support::Root::new();
    let delegate = DelegatedCgroup::open(&root.validated()).unwrap();
    let request = spec(
        mode,
        if mode == "cpu" { 500000 } else { 1000000 },
        if mode == "wall" { 350000 } else { 8000000 },
        if mode == "oom" { 32 << 20 } else { 64 << 20 },
    );
    let mut group = delegate.create_task(&request).unwrap();
    let path = root.path.join(group.name());
    let mut child = BlockedTarget::spawn(Path::new(&format!("/kf/bin/account-{mode}"))).unwrap();
    group.enroll(&mut child).unwrap();
    if mode == "cpu" {
        resume(&path, &child);
        let deadline = Instant::now() + Duration::from_secs(8);
        loop {
            if group.accounting_snapshot().unwrap().cpu_time_us() >= 25000
                && child.verify_stopped().is_ok()
            {
                break;
            }
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(2));
        }
        assert!(group.accounting_snapshot().unwrap().memory_peak_bytes() >= 8 << 20);
    }
    let before = group.accounting_snapshot().unwrap();
    let mut watch = BudgetSupervisor::prepare(&mut group, &request).unwrap();
    watch.mark_exec_release().unwrap();
    resume(&path, &child);
    let deadline = Instant::now() + Duration::from_secs(12);
    let mut observed_threads = false;
    let (usage, observations) = loop {
        if mode == "cpu"
            && fs::read_to_string(path.join("pids.current"))
                .unwrap()
                .trim()
                .parse::<u32>()
                .unwrap()
                >= 4
        {
            observed_threads = true;
        }
        match watch.poll().unwrap() {
            BudgetEvent::Finished {
                usage,
                observations,
            } => break (usage, observations),
            BudgetEvent::Running { .. } => {
                assert!(Instant::now() < deadline);
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    };
    if mode == "cpu" {
        assert!(
            observed_threads,
            "fixture never started its descendant threads"
        );
    }
    drop(watch);
    assert!(group.is_empty().unwrap());
    let status = child.terminate().unwrap();
    let reaped = orphans();
    let final_cpu = group.accounting_snapshot().unwrap().cpu_time_us();
    assert!(final_cpu >= before.cpu_time_us());
    group.remove_empty().unwrap();
    group.remove_empty().unwrap();
    assert!(!path.exists());
    (
        usage,
        observations,
        status.code().unwrap_or_else(|| -status.signal().unwrap()),
        reaped,
    )
}
#[test]
#[ignore = "independent capped VM: threaded descendant CPU/memory plus whole-cgroup termination"]
fn cpu_total_includes_initialization_threads_and_descendants() {
    let (u, o, status, reaped) = run("cpu");
    assert_eq!(status, -9);
    assert_eq!(reaped, 1);
    assert!(o.iter().any(|o| o.evidence.kind() == LimitKind::CpuTime));
    assert!(!o.iter().any(|o| o.evidence.kind() == LimitKind::WallTime));
    assert!(value(&u.cpu_time_us) >= 500000);
    assert!(value(&u.wall_time_us) > value(&u.cpu_time_us));
    assert!(value(&u.memory_peak_bytes) >= 16 << 20);
    println!(
        "KF_RESOURCE_PROOF mode=cpu charged_init=1 aggregate_threads_descendant=1 cpu_us={} wall_us={} peak_bytes={} cg_empty=1 owned_reaped=1",
        value(&u.cpu_time_us),
        value(&u.wall_time_us),
        value(&u.memory_peak_bytes)
    );
}
#[test]
#[ignore = "independent capped VM: monotonic deadline terminates sleeping tree"]
fn monotonic_wall_budget_does_not_use_cpu_bandwidth_as_total_time() {
    let (u, o, status, reaped) = run("wall");
    assert_eq!(status, -9);
    assert_eq!(reaped, 1);
    assert!(o.iter().any(|o| o.evidence.kind() == LimitKind::WallTime));
    assert!(!o.iter().any(|o| o.evidence.kind() == LimitKind::CpuTime));
    assert!(value(&u.wall_time_us) >= 350000);
    assert!(value(&u.cpu_time_us) < 1000000);
    println!(
        "KF_RESOURCE_PROOF mode=wall cpu_us={} wall_us={} cg_empty=1 owned_reaped=1",
        value(&u.cpu_time_us),
        value(&u.wall_time_us)
    );
}
#[test]
#[ignore = "independent capped VM: actual counters persist after threaded descendant exits"]
fn natural_exit_retains_aggregate_accounting_without_limit_fabrication() {
    let (u, o, status, reaped) = run("normal");
    assert_eq!(status, 7);
    assert_eq!(reaped, 0);
    assert!(o.is_empty());
    assert!(value(&u.cpu_time_us) >= 60000);
    assert!(value(&u.memory_peak_bytes) >= 8 << 20);
    println!(
        "KF_RESOURCE_PROOF mode=normal exit7=1 cpu_us={} peak_bytes={} cg_empty=1",
        value(&u.cpu_time_us),
        value(&u.memory_peak_bytes)
    );
}
#[test]
#[ignore = "independent capped VM: actual task OOM, no host memory pressure"]
fn oom_requires_task_events_and_group_kill_evidence() {
    let (u, o, status, reaped) = run("oom");
    assert_eq!(status, -9);
    assert_eq!(reaped, 1);
    assert!(o.iter().any(|o| o.evidence.kind() == LimitKind::Memory));
    let Measurement::Measured { value: e } = u.memory_events else {
        panic!("missing OOM counters")
    };
    assert!(e.max > 0 && e.oom > 0 && e.oom_kill >= 2 && e.oom_group_kill > 0);
    println!(
        "KF_RESOURCE_PROOF mode=oom max={} oom={} kills={} group_kills={} cg_empty=1",
        e.max, e.oom, e.oom_kill, e.oom_group_kill
    );
}
#[test]
#[ignore = "independent capped VM: real kernel policy change during monitor and fail-closed kill"]
fn changed_policy_terminates_before_user_release_and_reports_unavailable_meters() {
    support::require_vm();
    let root = support::Root::new();
    let delegate = DelegatedCgroup::open(&root.validated()).unwrap();
    let s = spec("changed-meter", 1000000, 8000000, 64 << 20);
    let mut group = delegate.create_task(&s).unwrap();
    let path = root.path.join(group.name());
    let mut child = BlockedTarget::spawn(Path::new("/kf/bin/account-wall")).unwrap();
    group.enroll(&mut child).unwrap();
    let mut watch = BudgetSupervisor::prepare(&mut group, &s).unwrap();
    watch.mark_exec_release().unwrap();
    fs::write(path.join("memory.max"), (32 << 20).to_string()).unwrap();
    let error = watch.poll().unwrap_err();
    assert!(error.termination_error.is_none());
    assert!(matches!(
        error.usage.cpu_time_us,
        Measurement::Unavailable { .. }
    ));
    drop(watch);
    assert!(group.is_empty().unwrap());
    assert_eq!(child.terminate().unwrap().signal(), Some(9));
    group.remove_empty().unwrap();
    println!(
        "KF_RESOURCE_PROOF mode=changed-policy no_fixture_release=1 fail_closed=1 meters_unavailable=1 cg_empty=1"
    );
}

#[test]
#[ignore = "independent capped VM: initialization exhausts CPU before exec release"]
fn exhausted_initialization_retains_actual_meters_without_starting_exec_clock() {
    support::require_vm();
    let root = support::Root::new();
    let delegate = DelegatedCgroup::open(&root.validated()).unwrap();
    let s = spec("init-budget", 20000, 8000000, 64 << 20);
    let mut group = delegate.create_task(&s).unwrap();
    let path = root.path.join(group.name());
    let mut child = BlockedTarget::spawn(Path::new("/kf/bin/account-cpu")).unwrap();
    group.enroll(&mut child).unwrap();
    resume(&path, &child);
    let deadline = Instant::now() + Duration::from_secs(8);
    while group.accounting_snapshot().unwrap().cpu_time_us() < 25000
        || child.verify_stopped().is_err()
    {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(2));
    }
    let e = match BudgetSupervisor::prepare(&mut group, &s) {
        Ok(_) => panic!("exhausted initialization released"),
        Err(e) => e,
    };
    assert_eq!(e.source.kind(), std::io::ErrorKind::TimedOut);
    assert!(e.termination_error.is_none());
    assert!(value(&e.usage.cpu_time_us) >= 20000);
    assert!(matches!(
        e.usage.wall_time_us,
        Measurement::Unavailable {
            reason: kf_sandbox::outcome::MeasurementFailure::NotStarted
        }
    ));
    assert!(group.is_empty().unwrap());
    assert_eq!(child.terminate().unwrap().signal(), Some(9));
    group.remove_empty().unwrap();
    println!(
        "KF_RESOURCE_PROOF mode=init-budget no_exec_release=1 charged_init=1 actual_meters=1 cg_empty=1"
    );
}
