#![cfg(target_os = "linux")]
//! Ordinary production adapter controlled by a trusted, independent VM fixture.
//! This ignored test never runs submitted source on the development host.
use kf_contracts::{judging as v1, judging_v2 as v2, judging_versions::NodeAdmission};
use kf_worker::{
    pipeline::LocalArtifacts,
    pipeline_v2::{Job, RuntimeAttempt},
    runtime_specs::AuthorizedJob,
};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

fn fixture_dir() -> PathBuf {
    assert_eq!(
        std::env::var("KF_SANDBOX_INDEPENDENT_VM").as_deref(),
        Ok("1")
    );
    assert_eq!(
        fs::read_to_string("/kf/INDEPENDENT_VM").unwrap().trim(),
        "isolated-test-guest"
    );
    assert!(
        fs::read_to_string("/proc/self/status")
            .unwrap()
            .lines()
            .any(|l| l.split_whitespace().collect::<Vec<_>>()
                == ["Uid:", "1000", "1000", "1000", "1000"])
    );
    assert_eq!(
        fs::read_dir("/sys/class/net")
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect::<Vec<_>>(),
        ["lo"]
    );
    for path in ["/mnt/hgfs", "/home/kronecker", "/root/.ssh"] {
        assert!(!std::path::Path::new(path).exists());
    }
    let mounts = fs::read_to_string("/proc/self/mountinfo").unwrap();
    assert!(
        ["hgfs", "9p", "virtiofs"]
            .iter()
            .all(|m| !mounts.contains(m))
    );
    let dir = PathBuf::from(std::env::var_os("KF_WORKER_CONTROL_DIR").unwrap());
    assert_eq!(
        fs::metadata(dir.join("admission.json")).unwrap().uid(),
        1000
    );
    dir
}

#[test]
#[ignore = "actual ordinary Worker cancellation only in independently capped no-NIC/no-share VM"]
fn runtime_cancel_from_trusted_control() {
    let dir = fixture_dir();
    let admission: NodeAdmission =
        v1::decode_wire(&fs::read(dir.join("admission.json")).unwrap()).unwrap();
    let job: Job = v1::decode_wire(&fs::read(dir.join("job.json")).unwrap()).unwrap();
    let authorized = AuthorizedJob::admit(
        job.manifest.clone(),
        job.binding.clone(),
        job.manifest_digest.clone(),
        job.attempt_number,
        &admission,
    )
    .unwrap();
    let cancel = Arc::new(AtomicBool::new(false));
    let finished = Arc::new(AtomicBool::new(false));
    let watch_cancel = cancel.clone();
    let watch_finished = finished.clone();
    let control = dir.join("cancel");
    let watcher = std::thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(110);
        while !watch_finished.load(Ordering::Acquire) {
            if fs::read(&control).is_ok_and(|b| b == b"cancel\n") {
                watch_cancel.store(true, Ordering::Release);
                return;
            }
            assert!(
                Instant::now() < deadline,
                "trusted fixture did not deliver cancellation"
            );
            std::thread::sleep(Duration::from_millis(5));
        }
    });
    let mut attempt = RuntimeAttempt::new(
        authorized,
        LocalArtifacts::open(&dir.join("artifacts")).unwrap(),
        "/var/lib/kf-judge/factory/control.sock".into(),
        dir.join("staging"),
        &cancel,
    )
    .unwrap();
    let result = kf_engine::submission_v2::judge_submission(
        &mut attempt,
        &job.manifest,
        job.binding,
        job.manifest_digest,
    )
    .unwrap();
    finished.store(true, Ordering::Release);
    watcher.join().unwrap();
    assert!(cancel.load(Ordering::Acquire));
    assert_eq!(result.report.verdict, v1::Verdict::Cancelled);
    assert!(result.report.cleanup_complete);
    assert!(result.report.failure_code.is_none());
    assert!(result.skipped.len() <= job.manifest.cases.len());
    println!(
        "KF_WORKER_CONTROL_PROOF {}",
        serde_json::to_string(&kf_worker::pipeline_v2::Judged {
            result,
            stages: attempt.into_stages()
        })
        .unwrap()
    );
}

#[test]
#[ignore = "actual prepared program seals and metadata fencing only in capped no-NIC/no-share VM"]
fn runtime_checked_program_survives_backing_mutation_and_rejects_forgery() {
    use kf_engine::submission_v2::{PreparationPort, RunPort};
    let dir = fixture_dir();
    let admission: NodeAdmission =
        v1::decode_wire(&fs::read(dir.join("admission.json")).unwrap()).unwrap();
    let job: Job = v1::decode_wire(&fs::read(dir.join("job.json")).unwrap()).unwrap();
    let authorized = AuthorizedJob::admit(
        job.manifest.clone(),
        job.binding.clone(),
        job.manifest_digest.clone(),
        job.attempt_number,
        &admission,
    )
    .unwrap();
    let cancel = AtomicBool::new(false);
    let mut attempt = RuntimeAttempt::new(
        authorized,
        LocalArtifacts::open(&dir.join("artifacts")).unwrap(),
        "/var/lib/kf-judge/factory/control.sock".into(),
        dir.join("staging"),
        &cancel,
    )
    .unwrap();
    let compiled = attempt
        .prepare(
            &job.manifest,
            &job.binding,
            &format!("c-{}", job.binding.attempt_id.as_uuid()),
        )
        .unwrap();
    assert_eq!(compiled.report.status, v1::CompilationStatus::Succeeded);
    assert!(compiled.cleanup_complete);
    let program = compiled.report.program.clone().unwrap();
    let run_id = format!("r-{}-0", job.binding.attempt_id.as_uuid());
    let mut bads = Vec::new();
    let mut bad = program.clone();
    bad.binding.fencing_version += 1;
    bads.push(bad);
    let mut bad = program.clone();
    bad.binding.attempt_id = "bbbbbbbb-bbbb-4bbb-8bbb-000000000001"
        .to_string()
        .try_into()
        .unwrap();
    bads.push(bad);
    let mut bad = program.clone();
    bad.source.digest = "f".repeat(64).try_into().unwrap();
    bads.push(bad);
    let mut bad = program.clone();
    bad.toolchain.image_revision += 1;
    bads.push(bad);
    let mut bad = program.clone();
    bad.kind = v2::ProgramKind::NativeExecutable;
    bads.push(bad);
    let mut bad = program.clone();
    bad.files[0].path = "../Main.class".into();
    bads.push(bad);
    let mut bad = program.clone();
    bad.files[0].artifact.bytes = 0;
    bads.push(bad);
    let mut bad = program.clone();
    bad.files[0].artifact.digest = "f".repeat(64).try_into().unwrap();
    bads.push(bad);
    let mut bad = program.clone();
    bad.files.push(bad.files[0].clone());
    bads.push(bad);
    let mut bad = program.clone();
    bad.files.clear();
    bads.push(bad);
    let mut bad = program.clone();
    bad.source.bytes = v1::MAX_SOURCE_BYTES + 1;
    bads.push(bad);
    for path in ["main.pyc", "program.zip"] {
        let mut bad = program.clone();
        bad.files[0].path = path.into();
        bads.push(bad);
    }
    let mut bad = program.clone();
    let file = bad.files[0].clone();
    bad.files = (0..257)
        .map(|i| {
            let mut file = file.clone();
            file.path = if i == 0 {
                program.files[0].path.clone()
            } else {
                format!("Extra{i}.class")
            };
            file
        })
        .collect();
    bads.push(bad);
    let rejected = bads.len();
    for bad in bads {
        let error = attempt
            .run(
                &bad,
                &job.manifest.cases[0],
                &job.manifest.run_limits,
                &run_id,
            )
            .err()
            .expect("forged program must be refused before Root");
        assert_eq!(error.code, v1::FailureCode::ArtifactUnavailable);
        assert!(error.cleanup_complete);
    }
    // Only the VM fixture's original backing artifact is changed. The ordinary
    // adapter must use its exact previously checked/compiled sealed capabilities.
    let backing = dir
        .join("artifacts")
        .join(job.manifest.source.digest.as_str());
    fs::write(
        &backing,
        b"corrupted source backing after actual preparation",
    )
    .unwrap();
    let mut observations = Vec::new();
    for (i, case) in job.manifest.cases.iter().enumerate() {
        let observation = attempt
            .run(
                &program,
                case,
                &job.manifest.run_limits,
                &format!("r-{}-{i}", job.binding.attempt_id.as_uuid()),
            )
            .unwrap();
        assert_eq!(
            observation.kind,
            kf_engine::ExecutionKind::Exited { code: 0 }
        );
        let expected = fs::read(dir.join("artifacts").join(case.expected.digest.as_str())).unwrap();
        assert_eq!(observation.stdout, expected);
        observations.push(serde_json::json!({"case_id":case.case_id,"cpu_time_us":observation.cpu_time_us,"memory_peak_bytes":observation.memory_peak_bytes,"exact_bytes":true}));
    }
    let stages = attempt.into_stages();
    assert_eq!(stages.len(), job.manifest.cases.len() + 1);
    println!(
        "KF_WORKER_SEALED_PROGRAM_PROOF {}",
        serde_json::json!({"program":program,"compilation":compiled.report,"stages":stages,"observations":observations,"rejected_metadata_mutations":rejected,"backing_changed_after_real_preparation":true,"same_rejected_run_id_later_executed":true})
    );
}
