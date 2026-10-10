//! Same-transaction final projections. An old unbound attempt stays unbound.
use crate::{
    database::{Error, Result},
    repository::hash,
};
use kf_contracts::{
    judging::Digest,
    judging_versions::{JobManifest, JudgementReport},
};
use serde_json::{Value, json};
use tokio_postgres::Transaction;

pub fn comparison_group(manifest: &JobManifest, epoch: &str) -> Result<String> {
    let _: Digest = epoch.to_owned().try_into().map_err(|_| Error::Invalid)?;
    let value = serde_json::to_value(manifest).map_err(|_| Error::Invalid)?;
    let group = json!({
        "version": 1, "measurement_epoch": epoch,
        "schema_version": value["schema_version"],
        "problem_version_id": value["problem_version_id"],
        "language": value["language"], "toolchain": value["toolchain"],
        "compile_limits": value["compile_limits"], "run_limits": value["run_limits"],
        "job_wall_time_us": value["job_wall_time_us"],
    });
    Ok(hash(
        &serde_json::to_vec(&group).map_err(|_| Error::Invalid)?,
    ))
}

pub async fn record(
    transaction: &Transaction<'_>,
    manifest: &JobManifest,
    report: &JudgementReport,
    digest: &str,
) -> Result<u64> {
    let fields = report.fields();
    let b = fields.binding;
    let Some(row) = transaction
        .query_opt(
            "SELECT a.measurement_epoch,s.id,s.language,e.updated_at FROM evaluations e \
         JOIN judge_attempts a ON a.id=e.current_attempt_id AND a.evaluation_id=e.id \
         JOIN submissions s ON s.id=e.submission_id WHERE e.id=$1 AND a.id=$2 \
         AND e.state='finished' AND e.verdict='accepted' AND e.failure_code IS NULL \
         AND a.cleanup_complete IS TRUE AND e.final_report_digest=$3 AND a.report_digest=$3",
            &[&b.evaluation_id.as_uuid(), &b.attempt_id.as_uuid(), &digest],
        )
        .await?
    else {
        return Ok(0);
    };
    let Some(epoch) = row.get::<_, Option<String>>("measurement_epoch") else {
        return Ok(0);
    };
    let group = comparison_group(manifest, &epoch)?;
    let value: Value = serde_json::to_value(report).map_err(|_| Error::Invalid)?;
    let measurement = |name: &str| -> Result<Option<i64>> {
        if value[name].is_null() {
            return Ok(None);
        }
        let value = value[name].as_u64().ok_or(Error::Invalid)?;
        Ok(Some(i64::try_from(value).map_err(|_| Error::Invalid)?))
    };
    let cpu = measurement("run_cpu_time_us")?;
    let memory = measurement("run_memory_peak_bytes")?;
    let submission: uuid::Uuid = row.get("id");
    let language: String = row.get("language");
    let finished: std::time::SystemTime = row.get("updated_at");
    Ok(transaction
        .execute(
            "INSERT INTO evaluation_performance(evaluation_id,submission_id,attempt_id,\
         problem_version_id,language,comparison_group,measurement_epoch,report_digest,\
         finished_at,cpu_time_us,memory_peak_bytes) \
         VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) ON CONFLICT(evaluation_id) DO NOTHING",
            &[
                &b.evaluation_id.as_uuid(),
                &submission,
                &b.attempt_id.as_uuid(),
                &manifest.problem_version_id().as_uuid(),
                &language,
                &group,
                &epoch,
                &digest,
                &finished,
                &cpu,
                &memory,
            ],
        )
        .await?)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn grouping_separates_exact_image_version_and_execution_budgets() {
        let bytes = include_bytes!(
            "../../../../tests/fixtures/contracts-v2/judge-manifest.valid-python313.json"
        );
        let value: Value = serde_json::from_slice(bytes).unwrap();
        let original: JobManifest = serde_json::from_value(value.clone()).unwrap();
        let epoch = "a".repeat(64);
        let original_group = comparison_group(&original, &epoch).unwrap();
        for (pointer, replacement) in [
            ("/toolchain/image_digest", json!("b".repeat(64))),
            (
                "/problem_version_id",
                json!("33333333-3333-4333-8333-333333333333"),
            ),
            ("/run_limits/cpu_time_us", json!(500_001)),
            ("/compile_limits/cpu_time_us", json!(5_000_001)),
            ("/job_wall_time_us", json!(120_000_001)),
        ] {
            let mut changed = value.clone();
            *changed.pointer_mut(pointer).unwrap() = replacement;
            let manifest: JobManifest = serde_json::from_value(changed).unwrap();
            assert_ne!(
                original_group,
                comparison_group(&manifest, &epoch).unwrap(),
                "{pointer} must isolate the cohort"
            );
        }
    }

    #[test]
    fn grouping_ignores_submission_identity_and_separates_epoch_and_language() {
        let bytes =
            include_bytes!("../../../../tests/fixtures/contracts/judge-manifest.valid.json");
        let mut value: Value = serde_json::from_slice(bytes).unwrap();
        let manifest: JobManifest = serde_json::from_value(value.clone()).unwrap();
        let epoch = "a".repeat(64);
        let group = comparison_group(&manifest, &epoch).unwrap();
        value["evaluation_id"] = json!("11111111-1111-4111-8111-111111111111");
        value["submission_id"] = json!("22222222-2222-4222-8222-222222222222");
        value["source"]["digest"] = json!("c".repeat(64));
        let other: JobManifest = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(group, comparison_group(&other, &epoch).unwrap());
        assert_ne!(group, comparison_group(&other, &"b".repeat(64)).unwrap());
        value["language"] = json!("c17");
        let other: JobManifest = serde_json::from_value(value).unwrap();
        assert_ne!(group, comparison_group(&other, &epoch).unwrap());
        assert!(comparison_group(&manifest, "unverified").is_err());
    }
}
