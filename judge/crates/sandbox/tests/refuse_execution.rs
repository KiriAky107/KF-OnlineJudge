use std::process::Command;

#[test]
fn execution_entrypoint_fails_closed() {
    let output = Command::new(env!("CARGO_BIN_EXE_kf-sandbox"))
        .arg("run")
        .output()
        .expect("sandbox binary must start");
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let error: serde_json::Value =
        serde_json::from_slice(&output.stderr).expect("structured refusal");
    assert_eq!(error["error"], "execution_configuration_required");
}
