//! Execution authority is applied under row locks, using database clock and fencing.
use crate::database::{Database, Error, Result};
use kf_contracts::judging_versions::{
    JobManifest, JudgementReport, LeaseGrant, NodeAdmission, StageReport, StoppedReport,
};
use kf_contracts::{SchemaVersion, judging::*, judging_v2 as v2};
use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};
use std::collections::HashMap;
use tokio_postgres::{Row, Transaction};
use uuid::Uuid;
#[derive(Clone)]
pub struct Repository {
    pub database: Database,
    pub admission: NodeAdmission,
    pub lease_seconds: u32,
    pub measurement_epochs: HashMap<Identifier, Digest>,
}
pub fn identifier(v: Uuid) -> Identifier {
    v.to_string().try_into().expect("non-nil generated UUID")
}
pub fn hash(data: &[u8]) -> String {
    format!("{:x}", Sha256::digest(data))
}
fn secret() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}
fn invalid(_: impl std::fmt::Debug) -> Error {
    Error::Invalid
}
fn as_value<T: serde::Serialize>(v: &T) -> Result<Value> {
    serde_json::to_value(v).map_err(invalid)
}
fn as_wire<T: serde::de::DeserializeOwned>(v: Value) -> Result<T> {
    let b = serde_json::to_vec(&v).map_err(invalid)?;
    decode_wire(&b).map_err(invalid)
}
impl Repository {
    async fn manifest(&self, t: &Transaction<'_>, e: &Row) -> Result<JobManifest> {
        let sid: Uuid = e.get("submission_id");
        let eid: Uuid = e.get("id");
        let h=t.query_opt("SELECT s.id,s.problem_version_id,s.language,s.source_digest,a.bytes,v.judging_schema_version,v.languages,v.language_configurations,v.toolchain,v.compile_limits,v.run_limits,v.job_wall_time_us FROM submissions s JOIN problem_versions v ON v.id=s.problem_version_id JOIN artifacts a ON a.digest=s.source_digest AND a.state='ready' WHERE s.id=$1 AND v.published_at IS NOT NULL",&[&sid]).await?.ok_or(Error::Invalid)?;
        let vid: Uuid = h.get("problem_version_id");
        let rows=t.query("SELECT c.id,c.position,c.input_digest,c.expected_digest,i.bytes input_bytes,o.bytes expected_bytes FROM problem_cases c JOIN artifacts i ON i.digest=c.input_digest AND i.state='ready' JOIN artifacts o ON o.digest=c.expected_digest AND o.state='ready' WHERE c.version_id=$1 ORDER BY c.position LIMIT 257",&[&vid]).await?;
        let mut cases = Vec::new();
        for (n, r) in rows.iter().enumerate() {
            if r.get::<_, i32>("position") != n as i32 {
                return Err(Error::Invalid);
            }
            cases.push(TestCase {
                case_id: identifier(r.get("id")),
                input: ArtifactReference {
                    digest: r
                        .get::<_, String>("input_digest")
                        .try_into()
                        .map_err(invalid)?,
                    bytes: r.get::<_, i64>("input_bytes") as u64,
                },
                expected: ArtifactReference {
                    digest: r
                        .get::<_, String>("expected_digest")
                        .try_into()
                        .map_err(invalid)?,
                    bytes: r.get::<_, i64>("expected_bytes") as u64,
                },
            });
        }
        let schema: i16 = h.get("judging_schema_version");
        let source = ArtifactReference {
            digest: h
                .get::<_, String>("source_digest")
                .try_into()
                .map_err(invalid)?,
            bytes: h.get::<_, i64>("bytes") as u64,
        };
        let m = if schema == 1 {
            JobManifest::V1(kf_contracts::judging::JobManifest {
                schema_version: SchemaVersion,
                kind: ManifestKind::Value,
                evaluation_id: identifier(eid),
                submission_id: identifier(sid),
                problem_version_id: identifier(vid),
                language: as_wire(json!(h.get::<_, String>("language")))?,
                toolchain: as_wire(h.get("toolchain"))?,
                source,
                cases,
                comparison: Comparison::Value,
                compile_limits: as_wire(h.get("compile_limits"))?,
                run_limits: as_wire(h.get("run_limits"))?,
                job_wall_time_us: h.get::<_, i64>("job_wall_time_us") as u64,
            })
        } else if schema == 2 {
            let configs: Vec<v2::LanguageConfiguration> = as_wire(
                h.get::<_, Option<Value>>("language_configurations")
                    .ok_or(Error::Invalid)?,
            )?;
            let languages: Vec<v2::Language> = as_wire(h.get("languages"))?;
            if configs.is_empty()
                || configs.len() > 4
                || configs.iter().map(|c| c.language).collect::<Vec<_>>() != languages
                || languages
                    .iter()
                    .collect::<std::collections::HashSet<_>>()
                    .len()
                    != languages.len()
                || ["toolchain", "compile_limits", "run_limits"]
                    .iter()
                    .any(|key| h.get::<_, Option<Value>>(*key).is_some())
                || h.get::<_, Option<i64>>("job_wall_time_us").is_some()
            {
                return Err(Error::Invalid);
            }
            let language: v2::Language = as_wire(json!(h.get::<_, String>("language")))?;
            let config = configs
                .into_iter()
                .find(|c| c.language == language)
                .ok_or(Error::Invalid)?;
            JobManifest::V2(v2::JobManifest {
                schema_version: v2::SchemaVersion,
                kind: ManifestKind::Value,
                evaluation_id: identifier(eid),
                submission_id: identifier(sid),
                problem_version_id: identifier(vid),
                language,
                source,
                cases,
                comparison: Comparison::Value,
                toolchain: config.toolchain,
                compile_limits: config.compile_limits,
                run_limits: config.run_limits,
                job_wall_time_us: config.job_wall_time_us,
            })
        } else {
            return Err(Error::Invalid);
        };
        // Canonicalization runs the selected generation's full semantic validators.
        m.canonical_bytes().map_err(invalid)?;
        Ok(m)
    }
    pub async fn claim(&self, eid: Identifier, worker: Identifier) -> Result<Option<LeaseGrant>> {
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        // A fixed node-capacity lock is separate from platform publication/digest locks.
        t.execute("SELECT pg_advisory_xact_lock(1262907471,0)", &[])
            .await?;
        let e=match t.query_opt("SELECT *,not_before<=clock_timestamp() due FROM evaluations WHERE id=$1 FOR UPDATE",&[&eid.as_uuid()]).await?{Some(e)=>e,None=>return Ok(None)};
        let max = (e.get::<_, i32>("max_attempts") as u32).min(self.admission.max_retries() + 1);
        if e.get::<_, String>("state") != "queued"
            || !e.get::<_, bool>("due")
            || e.get::<_, Option<std::time::SystemTime>>("cancel_requested_at")
                .is_some()
            || e.get::<_, i32>("attempts_count") as u32 >= max
        {
            return Ok(None);
        }
        let active:i64=t.query_one("SELECT count(*) FROM evaluations WHERE state IN ('claimed','compiling','running','cancelling')",&[]).await?.get(0);
        if active >= i64::from(self.admission.max_concurrent_jobs()) {
            return Err(Error::Busy);
        }
        let m = self.manifest(&t, &e).await?;
        let number = e.get::<_, i32>("attempts_count") + 1;
        self.admission.admit(&m, number as u32).map_err(invalid)?;
        let attempt = Uuid::new_v4();
        let token = secret();
        let fence = e
            .get::<_, i64>("fencing_version")
            .checked_add(1)
            .ok_or(Error::Invalid)?;
        let digest = hash(&m.canonical_bytes().map_err(invalid)?);
        let epoch = self.measurement_epochs.get(&worker).map(|e| e.as_str());
        let row=t.query_one("INSERT INTO judge_attempts(id,evaluation_id,worker_id,number,fencing_version,lease_token_hash,expires_at,manifest,manifest_digest,measurement_epoch) VALUES($1,$2,$3,$4,$5,$6,clock_timestamp()+$7::bigint*interval '1 second',$8,$9,$10) RETURNING floor(extract(epoch from expires_at)*1000)::bigint",&[&attempt,&eid.as_uuid(),&worker.as_uuid(),&number,&fence,&hash(token.as_bytes()),&i64::from(self.lease_seconds),&as_value(&m)?,&digest,&epoch]).await?;
        t.execute("UPDATE evaluations SET state='claimed',current_attempt_id=$2,attempts_count=$3,fencing_version=$4,updated_at=clock_timestamp() WHERE id=$1",&[&eid.as_uuid(),&attempt,&number,&fence]).await?;
        let grant = LeaseGrant::for_manifest(
            &m,
            LeaseBinding {
                evaluation_id: eid,
                attempt_id: identifier(attempt),
                worker_id: worker,
                fencing_version: fence as u64,
            },
            serde_json::from_value(json!(token)).map_err(invalid)?,
            number as u32,
            row.get::<_, i64>(0) as u64,
            digest.try_into().map_err(invalid)?,
        );
        t.commit().await?;
        Ok(Some(grant))
    }
    async fn bound(
        &self,
        t: &Transaction<'_>,
        b: &LeaseBinding,
        token: Option<&str>,
    ) -> Result<Row> {
        b.validate().map_err(invalid)?;
        let row=t.query_opt("SELECT e.*,a.worker_id,a.fencing_version attempt_fence,a.expires_at>clock_timestamp() live,a.revoked_at,a.lease_token_hash,a.manifest,a.manifest_digest,a.sequence,a.stage,a.case_index,a.report_digest,a.cleanup_complete FROM evaluations e JOIN judge_attempts a ON a.id=e.current_attempt_id AND a.evaluation_id=e.id WHERE e.id=$1 AND a.id=$2 FOR UPDATE OF e,a",&[&b.evaluation_id.as_uuid(),&b.attempt_id.as_uuid()]).await?.ok_or(Error::Rejected)?;
        if row.get::<_, Uuid>("worker_id") != b.worker_id.as_uuid()
            || row.get::<_, i64>("fencing_version") as u64 != b.fencing_version
            || row.get::<_, i64>("attempt_fence") as u64 != b.fencing_version
            || token.is_some_and(|s| hash(s.as_bytes()) != row.get::<_, String>("lease_token_hash"))
        {
            return Err(Error::Rejected);
        }
        Ok(row)
    }
    fn live(row: &Row) -> Result<()> {
        if !row.get::<_, bool>("live")
            || row
                .get::<_, Option<std::time::SystemTime>>("revoked_at")
                .is_some()
            || row
                .get::<_, Option<std::time::SystemTime>>("cancel_requested_at")
                .is_some()
            || !matches!(
                row.get::<_, String>("state").as_str(),
                "claimed" | "compiling" | "running"
            )
        {
            Err(Error::Rejected)
        } else {
            Ok(())
        }
    }
    pub async fn renew(&self, b: &LeaseBinding, token: &str) -> Result<u64> {
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let e = self.bound(&t, b, Some(token)).await?;
        Self::live(&e)?;
        let row=t.query_one("UPDATE judge_attempts SET expires_at=clock_timestamp()+$2::bigint*interval '1 second' WHERE id=$1 RETURNING floor(extract(epoch from expires_at)*1000)::bigint",&[&b.attempt_id.as_uuid(),&i64::from(self.lease_seconds)]).await?;
        let time = row.get::<_, i64>(0) as u64;
        t.commit().await?;
        Ok(time)
    }
    pub async fn progress(&self, b: &LeaseBinding, token: &str) -> Result<Value> {
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let e = self.bound(&t, b, Some(token)).await?;
        Self::live(&e)?;
        Ok(
            json!({"state":e.get::<_,String>("state"),"sequence":e.get::<_,i64>("sequence"),"stage":e.get::<_,Option<String>>("stage"),"case_index":e.get::<_,Option<i32>>("case_index")}),
        )
    }
    pub async fn stage(&self, report: &StageReport, token: &str) -> Result<()> {
        let fields = report.fields();
        let b = fields.binding;
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let e = self.bound(&t, b, Some(token)).await?;
        Self::live(&e)?;
        let m: JobManifest = as_wire(e.get("manifest"))?;
        report.validate_for_manifest(&m).map_err(invalid)?;
        let stage = match fields.stage {
            Stage::Compiling => "compiling",
            Stage::Running => "running",
        };
        let index = fields.case_index.map(i32::from);
        let sequence = e.get::<_, i64>("sequence");
        let old_stage = e.get::<_, Option<String>>("stage");
        let old_index = e.get::<_, Option<i32>>("case_index");
        if fields.sequence as i64 == sequence
            && old_stage.as_deref() == Some(stage)
            && old_index == index
        {
            return Ok(());
        }
        if fields.sequence as i64 != sequence.checked_add(1).ok_or(Error::Invalid)?
            || match fields.stage {
                Stage::Compiling => e.get::<_, String>("state") != "claimed" || old_stage.is_some(),
                Stage::Running => {
                    !matches!(
                        e.get::<_, String>("state").as_str(),
                        "compiling" | "running"
                    ) || index != Some(old_index.map_or(0, |v| v + 1))
                }
            }
        {
            return Err(Error::Rejected);
        }
        if index.is_some_and(|i| i as usize >= m.cases().len()) {
            return Err(Error::Rejected);
        }
        t.execute(
            "UPDATE judge_attempts SET sequence=$2,stage=$3,case_index=$4 WHERE id=$1",
            &[
                &b.attempt_id.as_uuid(),
                &(fields.sequence as i64),
                &stage,
                &index,
            ],
        )
        .await?;
        t.execute(
            "UPDATE evaluations SET state=$2,updated_at=clock_timestamp() WHERE id=$1",
            &[&b.evaluation_id.as_uuid(), &stage],
        )
        .await?;
        t.commit().await?;
        Ok(())
    }
    async fn finish(
        &self,
        t: &Transaction<'_>,
        e: &Row,
        b: &LeaseBinding,
        verdict: &str,
        failure: Option<&str>,
        clean: bool,
    ) -> Result<()> {
        let cancelled = e
            .get::<_, Option<std::time::SystemTime>>("cancel_requested_at")
            .is_some();
        let retries =
            (e.get::<_, i32>("max_attempts") as u32).min(self.admission.max_retries() + 1);
        let retry = !cancelled
            && clean
            && verdict == "system_error"
            && e.get::<_, i32>("attempts_count") < retries as i32;
        let (state, v, code) = if !clean {
            ("cancelling", "system_error", Some("cleanup_failed"))
        } else if cancelled {
            ("cancelled", "cancelled", None)
        } else if retry {
            ("queued", verdict, failure)
        } else if verdict == "cancelled" {
            ("cancelled", verdict, None)
        } else if verdict == "system_error" {
            ("failed", verdict, failure)
        } else {
            ("finished", verdict, None)
        };
        let delay = 2_i64.pow((e.get::<_, i32>("attempts_count") as u32).min(5));
        t.execute("UPDATE judge_attempts SET cleanup_complete=$2,revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE id=$1",&[&b.attempt_id.as_uuid(),&clean]).await?;
        t.execute("UPDATE evaluations SET state=$2,verdict=$3,failure_code=$4,not_before=clock_timestamp()+$5::bigint*interval '1 second',updated_at=clock_timestamp() WHERE id=$1",&[&b.evaluation_id.as_uuid(),&state,&v,&code,&delay]).await?;
        if retry {
            t.execute("UPDATE evaluations SET verdict=NULL,failure_code=NULL,final_report=NULL,final_report_digest=NULL WHERE id=$1",&[&b.evaluation_id.as_uuid()]).await?;
            t.execute("UPDATE outbox SET delivered_at=NULL,next_attempt_at=clock_timestamp()+$2::bigint*interval '1 second' WHERE evaluation_id=$1",&[&b.evaluation_id.as_uuid(),&delay]).await?;
        }
        Ok(())
    }
    pub async fn report(&self, r: &JudgementReport, token: &str) -> Result<()> {
        let fields = r.fields();
        let b = fields.binding;
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let e = self.bound(&t, b, Some(token)).await?;
        let value = as_value(r)?;
        let digest = hash(&serde_json::to_vec(&value).map_err(invalid)?);
        if let Some(old) = e.get::<_, Option<String>>("report_digest") {
            if old == digest {
                return Ok(());
            } else {
                return Err(Error::Rejected);
            }
        }
        Self::live(&e)?;
        let m: JobManifest = as_wire(e.get("manifest"))?;
        let md: Digest = e
            .get::<_, String>("manifest_digest")
            .try_into()
            .map_err(invalid)?;
        r.validate_for_manifest(&m, &md).map_err(invalid)?;
        r.validate_program_budget(&m).map_err(invalid)?;
        if !matches!(fields.verdict, Verdict::SystemError | Verdict::Cancelled)
            && (e.get::<_, String>("state") == "claimed"
                || (fields.compilation_status == CompilationStatus::Succeeded
                    && e.get::<_, String>("state") != "running"))
        {
            return Err(Error::Rejected);
        }
        if !fields.cases.is_empty() {
            let index = e
                .get::<_, Option<i32>>("case_index")
                .ok_or(Error::Rejected)? as usize;
            if fields.cases.len() > index + 1
                || (!matches!(fields.verdict, Verdict::SystemError | Verdict::Cancelled)
                    && fields.cases.len() != index + 1)
            {
                return Err(Error::Rejected);
            }
        }
        let vid = m.problem_version_id().as_uuid();
        for (position, c) in fields.cases.iter().enumerate() {
            let verdict = serde_json::to_value(c.verdict).map_err(invalid)?;
            t.execute("INSERT INTO case_results(attempt_id,case_id,evaluation_id,problem_version_id,position,verdict,cpu_time_us,memory_peak_bytes) VALUES($1,$2,$3,$4,$5,$6,$7,$8)",&[&b.attempt_id.as_uuid(),&c.case_id.as_uuid(),&b.evaluation_id.as_uuid(),&vid,&(position as i32),&verdict.as_str().ok_or(Error::Invalid)?,&c.cpu_time_us.map(|v|v as i64),&c.memory_peak_bytes.map(|v|v as i64)]).await?;
        }
        t.execute(
            "UPDATE judge_attempts SET report=$2,report_digest=$3 WHERE id=$1",
            &[&b.attempt_id.as_uuid(), &value, &digest],
        )
        .await?;
        t.execute(
            "UPDATE evaluations SET final_report=$2,final_report_digest=$3 WHERE id=$1",
            &[&b.evaluation_id.as_uuid(), &value, &digest],
        )
        .await?;
        let verdict = serde_json::to_value(fields.verdict).map_err(invalid)?;
        let code = serde_json::to_value(fields.failure_code).map_err(invalid)?;
        self.finish(
            &t,
            &e,
            b,
            verdict.as_str().ok_or(Error::Invalid)?,
            code.as_str(),
            fields.cleanup_complete,
        )
        .await?;
        crate::performance::record(&t, &m, r, &digest).await?;
        t.commit().await?;
        Ok(())
    }
    pub async fn backfill_performance(&self, evaluation: Identifier) -> Result<u64> {
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let row = t
            .query_opt(
                "SELECT final_report FROM evaluations WHERE id=$1 FOR UPDATE",
                &[&evaluation.as_uuid()],
            )
            .await?
            .ok_or(Error::Rejected)?;
        let value: Value = row.get::<_, Option<Value>>(0).ok_or(Error::Rejected)?;
        let report: JudgementReport = as_wire(value.clone())?;
        let b = report.fields().binding;
        if b.evaluation_id != evaluation {
            return Err(Error::Rejected);
        }
        let e = self.bound(&t, b, None).await?;
        let manifest: JobManifest = as_wire(e.get("manifest"))?;
        let manifest_digest: Digest = e
            .get::<_, String>("manifest_digest")
            .try_into()
            .map_err(invalid)?;
        report
            .validate_for_manifest(&manifest, &manifest_digest)
            .map_err(invalid)?;
        report.validate_program_budget(&manifest).map_err(invalid)?;
        let digest = hash(&serde_json::to_vec(&as_value(&report)?).map_err(invalid)?);
        if e.get::<_, Option<String>>("final_report_digest").as_deref() != Some(&digest)
            || e.get::<_, Option<String>>("report_digest").as_deref() != Some(&digest)
        {
            return Err(Error::Rejected);
        }
        let count = crate::performance::record(&t, &manifest, &report, &digest).await?;
        t.commit().await?;
        Ok(count)
    }
    pub async fn cancel(&self, eid: Identifier) -> Result<()> {
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let e = t
            .query_opt(
                "SELECT state,current_attempt_id FROM evaluations WHERE id=$1 FOR UPDATE",
                &[&eid.as_uuid()],
            )
            .await?
            .ok_or(Error::Rejected)?;
        let state = e.get::<_, String>("state");
        if state == "queued" {
            t.execute("UPDATE evaluations SET state='cancelled',verdict='cancelled',failure_code=NULL,final_report=NULL,final_report_digest=NULL,cancel_requested_at=clock_timestamp(),updated_at=clock_timestamp() WHERE id=$1",&[&eid.as_uuid()]).await?;
        } else if matches!(
            state.as_str(),
            "claimed" | "compiling" | "running" | "cancelling"
        ) {
            t.execute("UPDATE evaluations SET state='cancelling',cancel_requested_at=coalesce(cancel_requested_at,clock_timestamp()),updated_at=clock_timestamp() WHERE id=$1",&[&eid.as_uuid()]).await?;
            t.execute("UPDATE judge_attempts SET revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE id=$1",&[&e.get::<_,Option<Uuid>>("current_attempt_id")]).await?;
        }
        t.commit().await?;
        Ok(())
    }
    pub async fn stopped(&self, r: &StoppedReport) -> Result<()> {
        let b = r.binding();
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let e = self.bound(&t, b, None).await?;
        let m: JobManifest = as_wire(e.get("manifest"))?;
        r.validate_for_manifest(&m).map_err(invalid)?;
        if e.get::<_, String>("state") != "cancelling" {
            if r.cleanup_complete() && e.get::<_, Option<bool>>("cleanup_complete") == Some(true) {
                return Ok(());
            }
            return Err(Error::Rejected);
        }
        if e.get::<_, Option<std::time::SystemTime>>("revoked_at")
            .is_none()
        {
            return Err(Error::Rejected);
        }
        let code = e.get::<_, Option<String>>("failure_code");
        self.finish(
            &t,
            &e,
            b,
            "system_error",
            Some(code.as_deref().unwrap_or("lease_lost")),
            r.cleanup_complete(),
        )
        .await?;
        t.commit().await?;
        Ok(())
    }
    pub async fn pending_stops(&self, worker: Identifier) -> Result<Value> {
        let s = self.database.session().await?;
        let rows=s.client.query("SELECT e.id,a.id attempt_id,a.fencing_version,jsonb_array_length(a.manifest->'cases') case_count,(a.manifest->>'schema_version')::int schema_version FROM evaluations e JOIN judge_attempts a ON a.id=e.current_attempt_id WHERE e.state='cancelling' AND a.worker_id=$1 ORDER BY e.updated_at,e.id LIMIT 16",&[&worker.as_uuid()]).await?;
        let entries = rows.iter().map(|r| {
            let mut entry=json!({"binding":LeaseBinding{evaluation_id:identifier(r.get("id")),attempt_id:identifier(r.get("attempt_id")),worker_id:worker,fencing_version:r.get::<_,i64>("fencing_version")as u64},"case_count":r.get::<_,i32>("case_count")});
            match r.get::<_,i32>("schema_version") {
                // Preserve the old pending-stop response and all v1 report bytes.
                1 => {},
                2 => entry["schema_version"] = json!(2),
                _ => return Err(Error::Invalid),
            }
            Ok(entry)
        }).collect::<Result<Vec<_>>>()?;
        Ok(Value::Array(entries))
    }
    pub async fn recover(&self) -> Result<u64> {
        let mut s = self.database.session().await?;
        let t = s.client.transaction().await?;
        let rows=t.query("SELECT e.id,e.current_attempt_id FROM evaluations e JOIN judge_attempts a ON a.id=e.current_attempt_id WHERE e.state IN ('claimed','compiling','running') AND (a.expires_at<=clock_timestamp() OR a.revoked_at IS NOT NULL) ORDER BY a.expires_at,e.id FOR UPDATE OF e SKIP LOCKED LIMIT 16",&[]).await?;
        for r in &rows {
            t.execute("UPDATE evaluations SET state='cancelling',failure_code='lease_lost',updated_at=clock_timestamp() WHERE id=$1",&[&r.get::<_,Uuid>("id")]).await?;
            t.execute("UPDATE judge_attempts SET revoked_at=coalesce(revoked_at,clock_timestamp()) WHERE id=$1",&[&r.get::<_,Uuid>("current_attempt_id")]).await?;
        }
        // Re-notify queued rows if an earlier delivery was acknowledged without a claim.
        // This is periodic bounded compensation, never execution authority or a busy loop.
        let queued=t.query("SELECT e.id FROM evaluations e JOIN outbox o ON o.evaluation_id=e.id WHERE e.state='queued' AND e.not_before<=clock_timestamp() AND o.delivered_at<clock_timestamp()-interval '30 seconds' ORDER BY e.not_before,e.id FOR UPDATE OF e SKIP LOCKED LIMIT 16",&[]).await?;
        for r in &queued {
            t.execute("UPDATE outbox SET delivered_at=NULL,next_attempt_at=clock_timestamp() WHERE evaluation_id=$1",&[&r.get::<_,Uuid>("id")]).await?;
        }
        let count = (rows.len() + queued.len()) as u64;
        t.commit().await?;
        Ok(count)
    }
}
