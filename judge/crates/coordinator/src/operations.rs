//! Bounded read-only execution counters; no task identifiers or authority writes.
use crate::database::{Database, Error, Result};
use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct BoundedCount {
    pub count_capped: u16,
    pub truncated: bool,
}
impl BoundedCount {
    fn new(count: i64) -> Result<Self> {
        if !(0..=129).contains(&count) {
            return Err(Error::Invalid);
        }
        Ok(Self {
            count_capped: count.min(128) as u16,
            truncated: count == 129,
        })
    }
}
#[derive(Debug, Serialize)]
pub struct ExecutionCounters {
    pub queued: BoundedCount,
    pub in_flight: BoundedCount,
    pub cleanup_unconfirmed: BoundedCount,
    pub database_clock_unix_ms: i64,
}
pub async fn snapshot(database: &Database) -> Result<ExecutionCounters> {
    let mut session = database.session().await?;
    let transaction = session
        .client
        .build_transaction()
        .read_only(true)
        .isolation_level(tokio_postgres::IsolationLevel::RepeatableRead)
        .start()
        .await?;
    // LIMIT applies inside each count: a timeout or truncated projection can
    // never be interpreted as empty capacity. The fixed DB deadline still
    // bounds a planner that cannot produce the first 129 rows cheaply.
    let row = transaction.query_one(
        "SELECT
          (SELECT count(*) FROM (SELECT 1 FROM evaluations WHERE state='queued' LIMIT 129) q) queued,
          (SELECT count(*) FROM (SELECT 1 FROM evaluations WHERE state IN
            ('claimed','compiling','running','cancelling') LIMIT 129) a) in_flight,
          (SELECT count(*) FROM (SELECT 1 FROM evaluations e JOIN judge_attempts a
            ON e.current_attempt_id=a.id AND a.evaluation_id=e.id
            WHERE a.cleanup_complete IS DISTINCT FROM TRUE LIMIT 129) c) cleanup_unconfirmed,
          floor(extract(epoch FROM clock_timestamp())*1000)::bigint database_clock_unix_ms",
        &[],
    ).await?;
    let value = ExecutionCounters {
        queued: BoundedCount::new(row.get("queued"))?,
        in_flight: BoundedCount::new(row.get("in_flight"))?,
        cleanup_unconfirmed: BoundedCount::new(row.get("cleanup_unconfirmed"))?,
        database_clock_unix_ms: row.get("database_clock_unix_ms"),
    };
    transaction.commit().await?;
    Ok(value)
}

pub async fn absent_notification_id(database: &Database) -> Result<uuid::Uuid> {
    let session = database.session().await?;
    for _ in 0..4 {
        let identifier = uuid::Uuid::new_v4();
        if session
            .client
            .query_opt("SELECT 1 FROM evaluations WHERE id=$1", &[&identifier])
            .await?
            .is_none()
        {
            return Ok(identifier);
        }
    }
    Err(Error::Unavailable)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn truncated_counts_never_become_an_empty_drain_proof() {
        let empty = BoundedCount::new(0).unwrap();
        assert_eq!(empty.count_capped, 0);
        assert!(!empty.truncated);
        let crowded = BoundedCount::new(129).unwrap();
        assert_eq!(crowded.count_capped, 128);
        assert!(crowded.truncated);
        assert!(BoundedCount::new(-1).is_err());
        assert!(BoundedCount::new(130).is_err());
    }
}
