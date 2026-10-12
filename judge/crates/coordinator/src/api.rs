use crate::{config::Config, database::Error, repository::Repository};
use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Request, State},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use kf_contracts::judging_versions::{JudgementReport, StageReport, StoppedReport};
use kf_contracts::{JudgeTaskNotification, judging::*};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use tokio::sync::Semaphore;
#[derive(Clone)]
pub struct App {
    pub repository: Repository,
    pub config: Arc<Config>,
    slots: Arc<Semaphore>,
}
impl App {
    pub fn new(repository: Repository, config: Config) -> Self {
        Self {
            repository,
            config: Arc::new(config),
            slots: Arc::new(Semaphore::new(16)),
        }
    }
}
type ApiResult<T> = Result<T, ApiError>;
pub struct ApiError(Error);
impl From<Error> for ApiError {
    fn from(e: Error) -> Self {
        Self(e)
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let (status, code) = match self.0 {
            Error::Busy => (StatusCode::TOO_MANY_REQUESTS, "capacity"),
            Error::Unavailable => (StatusCode::SERVICE_UNAVAILABLE, "unavailable"),
            Error::Rejected => (StatusCode::CONFLICT, "stale_or_foreign_authority"),
            Error::Invalid => (StatusCode::UNPROCESSABLE_ENTITY, "invalid_request"),
        };
        (status, Json(json!({"error":code}))).into_response()
    }
}
fn token(headers: &HeaderMap) -> Option<&str> {
    headers
        .get("authorization")?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
}
fn equal(a: &str, b: &str) -> bool {
    a.len() == 64
        && b.len() == 64
        && a.as_bytes()
            .iter()
            .zip(b.as_bytes())
            .fold(0u8, |v, (a, b)| v | (a ^ b))
            == 0
}
fn worker(app: &App, h: &HeaderMap) -> ApiResult<Identifier> {
    let token = token(h).ok_or(ApiError(Error::Rejected))?;
    app.config
        .workers
        .iter()
        .find(|w| equal(token, w.token.expose_secret()))
        .map(|w| w.id)
        .ok_or(ApiError(Error::Rejected))
}
fn platform(app: &App, h: &HeaderMap) -> ApiResult<()> {
    if token(h).is_some_and(|v| equal(v, app.config.platform_token.expose_secret())) {
        Ok(())
    } else {
        Err(ApiError(Error::Rejected))
    }
}
fn lease(h: &HeaderMap) -> ApiResult<&str> {
    let v = h
        .get("x-lease-token")
        .and_then(|v| v.to_str().ok())
        .ok_or(ApiError(Error::Rejected))?;
    if v.len() != 64 {
        return Err(ApiError(Error::Rejected));
    }
    Ok(v)
}
fn decode<T: serde::de::DeserializeOwned>(body: &Bytes) -> ApiResult<T> {
    decode_wire(body).map_err(|_| ApiError(Error::Invalid))
}
fn bound_worker(app: &App, h: &HeaderMap, b: &LeaseBinding) -> ApiResult<()> {
    if worker(app, h)? != b.worker_id {
        return Err(ApiError(Error::Rejected));
    }
    Ok(())
}
async fn guard(State(app): State<App>, req: Request, next: Next) -> Response {
    if req.uri().path().starts_with("/internal/")
        && !(worker(&app, req.headers()).is_ok() || platform(&app, req.headers()).is_ok())
    {
        return (
            StatusCode::UNAUTHORIZED,
            Json(json!({"error":"service_identity_required"})),
        )
            .into_response();
    }
    let Ok(_slot) = app.slots.clone().try_acquire_owned() else {
        return ApiError(Error::Busy).into_response();
    };
    match tokio::time::timeout(Duration::from_secs(10), next.run(req)).await {
        Ok(mut response) => {
            response.headers_mut().insert(
                "cache-control",
                "no-store".parse().expect("constant header"),
            );
            response
        }
        Err(_) => (
            StatusCode::REQUEST_TIMEOUT,
            Json(json!({"error":"request_deadline"})),
        )
            .into_response(),
    }
}
async fn live() -> Json<Value> {
    Json(json!({"status":"ok","service":"coordinator","version":env!("CARGO_PKG_VERSION")}))
}
async fn ready(State(app): State<App>) -> ApiResult<Json<Value>> {
    app.repository.database.ready().await?;
    Ok(Json(json!({"status":"ready"})))
}
async fn operations(State(app): State<App>, headers: HeaderMap) -> ApiResult<Json<Value>> {
    // The general internal guard also recognizes Workers; this projection is
    // separately restricted to platform/operator authority, never worker tokens.
    platform(&app, &headers)?;
    let counters = crate::operations::snapshot(&app.repository.database).await?;
    Ok(Json(json!({
        "schema_version": 1,
        "admission": app.config.admission,
        "configured_workers": app.config.workers.len(),
        "lease_seconds": app.config.lease_seconds,
        "counters": counters,
    })))
}
async fn notification_probe_id(
    State(app): State<App>,
    headers: HeaderMap,
) -> ApiResult<Json<Value>> {
    platform(&app, &headers)?;
    let id = crate::operations::absent_notification_id(&app.repository.database).await?;
    Ok(Json(
        json!({"schema_version":1,"evaluation_id":id,"absent":true}),
    ))
}
async fn claim(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<Response> {
    let w = worker(&app, &h)?;
    let n: JudgeTaskNotification = decode(&b)?;
    let eid = n
        .evaluation_id
        .to_string()
        .try_into()
        .map_err(|_| ApiError(Error::Invalid))?;
    Ok(match app.repository.claim(eid, w).await? {
        Some(g) => Json(g).into_response(),
        None => StatusCode::NO_CONTENT.into_response(),
    })
}
async fn renew(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<Json<Value>> {
    let binding: LeaseBinding = decode(&b)?;
    bound_worker(&app, &h, &binding)?;
    Ok(Json(
        json!({"expires_at_unix_ms":app.repository.renew(&binding,lease(&h)?).await?}),
    ))
}
async fn progress(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<Json<Value>> {
    let binding: LeaseBinding = decode(&b)?;
    bound_worker(&app, &h, &binding)?;
    Ok(Json(app.repository.progress(&binding, lease(&h)?).await?))
}
async fn stage(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<StatusCode> {
    let r: StageReport = decode(&b)?;
    bound_worker(&app, &h, r.fields().binding)?;
    app.repository.stage(&r, lease(&h)?).await?;
    Ok(StatusCode::NO_CONTENT)
}
async fn report(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<StatusCode> {
    let r: JudgementReport = decode(&b)?;
    bound_worker(&app, &h, r.fields().binding)?;
    app.repository.report(&r, lease(&h)?).await?;
    Ok(StatusCode::NO_CONTENT)
}
async fn stopped(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<StatusCode> {
    let r: StoppedReport = decode(&b)?;
    bound_worker(&app, &h, r.binding())?;
    app.repository.stopped(&r).await?;
    Ok(StatusCode::NO_CONTENT)
}
async fn pending(State(app): State<App>, h: HeaderMap) -> ApiResult<Json<Value>> {
    Ok(Json(app.repository.pending_stops(worker(&app, &h)?).await?))
}
async fn cancel(State(app): State<App>, h: HeaderMap, b: Bytes) -> ApiResult<StatusCode> {
    platform(&app, &h)?;
    let n: JudgeTaskNotification = decode(&b)?;
    app.repository
        .cancel(
            n.evaluation_id
                .to_string()
                .try_into()
                .map_err(|_| ApiError(Error::Invalid))?,
        )
        .await?;
    Ok(StatusCode::NO_CONTENT)
}
pub fn router() -> Router {
    Router::new().route("/health/live", get(live))
}
pub fn application(app: App) -> Router {
    Router::new()
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
        .route("/internal/operations/status", get(operations))
        .route("/internal/operations/probe-id", get(notification_probe_id))
        .route("/internal/claim", post(claim))
        .route("/internal/renew", post(renew))
        .route("/internal/stage", post(stage))
        .route("/internal/progress", post(progress))
        .route("/internal/report", post(report))
        .route("/internal/stopped", post(stopped))
        .route("/internal/pending-stops", get(pending))
        .route("/internal/cancel", post(cancel))
        .layer(DefaultBodyLimit::max(MAX_WIRE_BYTES))
        .layer(middleware::from_fn_with_state(app.clone(), guard))
        .with_state(app)
}
