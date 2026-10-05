use kf_contracts::JudgeTaskNotification;
use std::{fs, path::Path};

#[test]
fn shared_examples_match_python_contract() {
    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/contracts");
    let mut count = 0;
    for entry in fs::read_dir(fixtures).expect("shared fixture directory must exist") {
        let path = entry.expect("fixture entry").path();
        let name = path.file_name().expect("fixture name").to_string_lossy();
        if !name.starts_with("judge-task.") || !name.ends_with(".json") {
            continue;
        }
        count += 1;
        let text = fs::read_to_string(&path).expect("fixture is UTF-8");
        let decoded = serde_json::from_str::<JudgeTaskNotification>(&text);
        if name.contains(".invalid-") {
            assert!(decoded.is_err(), "must reject {name}");
        } else {
            let notification = decoded.unwrap_or_else(|error| panic!("{name}: {error}"));
            let original: serde_json::Value = serde_json::from_str(&text).expect("valid JSON");
            assert_eq!(serde_json::to_value(notification).unwrap(), original);
        }
    }
    assert!(
        count >= 5,
        "shared acceptance/rejection cases must not disappear"
    );
}
