use kf_contracts::restore_io::{decode_reply, decode_request};
use serde::Deserialize;

#[derive(Deserialize)]
struct Case {
    name: String,
    direction: String,
    valid: bool,
    value: serde_json::Value,
}

#[test]
fn shared_restore_io_examples() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../../tests/fixtures/restore-io/examples.json");
    let cases: Vec<Case> = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    assert_eq!(cases.len(), 46);
    for case in cases {
        let raw = serde_json::to_vec(&case.value).unwrap();
        let result = match case.direction.as_str() {
            "request" => decode_request(&raw).map(|v| serde_json::to_value(v).unwrap()),
            "reply" => decode_reply(&raw).map(|v| serde_json::to_value(v).unwrap()),
            _ => panic!("unknown fixture direction"),
        };
        assert_eq!(result.is_ok(), case.valid, "{}", case.name);
        if let Ok(actual) = result {
            assert_eq!(actual, case.value, "{}", case.name);
        }
    }
}

#[test]
fn duplicate_fields_and_oversized_wire_are_refused() {
    assert!(
        decode_request(br#"{"kind":"restore.io.finish","schema_version":1,"schema_version":1}"#)
            .is_err()
    );
    assert!(decode_request(&vec![b' '; 256 * 1024 + 1]).is_err());
    assert!(decode_reply(&vec![b' '; 64 * 1024 + 1]).is_err());
}
