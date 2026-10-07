//! Fixed VM-only preparation cancellation/exception matrix using actual owners.
use super::{
    blocked::PreparedTarget, identity::IdentityReducedTarget, inputs::InputBoundTarget,
    namespaces::NamespacedTarget, proc_dev::RestrictedRootTarget, reap::OwnedChildren,
    root::RootedTarget, seccomp::FilteredTarget,
};
use crate::{artifacts::ArtifactRoot, runtime::ValidatedRuntimeConfig, spec::ValidatedSpec};
use std::{io, path::Path};
fn fail(e: super::blocked::InitializationFailure) -> io::Error {
    io::Error::other(format!(
        "{:?} {:?} cleanup {:?}",
        e.failure.stage, e.failure.errno, e.cleanup_failure
    ))
}
pub(crate) fn cancel_stage(
    stage: &str,
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    binary: &Path,
    artifacts: &ArtifactRoot,
) -> io::Result<serde_json::Value> {
    let mut children = OwnedChildren::begin()?;
    match stage {
        "blocked" => {
            let mut t = PreparedTarget::prepare(config, spec, binary).map_err(fail)?;
            let first = t.refuse_at(crate::outcome::ExecutionStage::Preparation, None);
            let second = t.refuse_at(crate::outcome::ExecutionStage::Preparation, None);
            if first.cleanup_failure.is_some() || second.cleanup_failure.is_some() {
                return Err(io::Error::other("idempotent stopped owner cleanup"));
            }
        }
        "namespace" => {
            drop(NamespacedTarget::prepare(config, spec, binary).map_err(fail)?);
        }
        "root" => {
            drop(RootedTarget::prepare(config, spec, binary).map_err(fail)?);
        }
        "proc" => {
            drop(RestrictedRootTarget::prepare(config, spec, binary).map_err(fail)?);
        }
        "inputs" => {
            drop(InputBoundTarget::prepare(config, spec, binary, artifacts).map_err(fail)?);
        }
        "identity" => {
            drop(IdentityReducedTarget::prepare(config, spec, binary, artifacts).map_err(fail)?);
        }
        "seccomp" => {
            drop(FilteredTarget::prepare(config, spec, binary, artifacts).map_err(fail)?);
        }
        "io" | "exec" => {
            let mut t = FilteredTarget::prepare(config, spec, binary, artifacts).map_err(fail)?;
            super::capture::private_supervisor()?;
            let (mut lease, dir) = super::capture::CaptureLease::prepare(config, spec)?;
            let (pump, stdio) = crate::io::PipePump::prepare(t.take_stdin(), spec, dir)?;
            if stage == "exec" {
                t.root_mut().target.prepare_execution(spec, stdio)?;
            } else {
                drop(stdio);
            }
            let result = t.refuse_at(crate::outcome::ExecutionStage::Exec, None);
            if result.cleanup_failure.is_some() {
                return Err(fail(result));
            }
            drop(pump);
            lease.cleanup()?;
            lease.cleanup()?;
        }
        _ => return Err(io::Error::other("fixed preparation stage")),
    }
    let n = children.finish()?;
    Ok(
        serde_json::json!({"stage":stage,"cancel_before_exec":true,"remaining_owned_children":0,"reaped_after_drop":n,"idempotent":matches!(stage,"blocked"|"io"|"exec")}),
    )
}
pub(crate) fn fail_stage(
    stage: &str,
    config: &ValidatedRuntimeConfig,
    spec: &ValidatedSpec,
    binary: &Path,
    artifacts: &ArtifactRoot,
) -> io::Result<serde_json::Value> {
    if stage == "io" {
        let Err(e) = super::run::execute(
            config,
            spec,
            binary,
            artifacts,
            super::run::MonitorMode::Normal,
        ) else {
            return Err(io::Error::other("expected pipe budget failure"));
        };
        if e.failure.stage != crate::outcome::ExecutionStage::Exec || e.cleanup_failure.is_some() {
            return Err(fail(e));
        }
        return Ok(
            serde_json::json!({"stage":stage,"failure":e.failure,"cleanup_complete":true,"user_exec_released":false}),
        );
    }
    let mut children = OwnedChildren::begin()?;
    let result = if stage == "inputs" {
        let target = RestrictedRootTarget::prepare(config, spec, binary).map_err(fail)?;
        InputBoundTarget::bind(target, artifacts).map(|t| {
            drop(t);
        })
    } else {
        FilteredTarget::prepare(config, spec, binary, artifacts).map(|t| {
            drop(t);
        })
    };
    let Err(e) = result else {
        return Err(io::Error::other("expected actual preparation failure"));
    };
    if e.cleanup_failure.is_some() {
        return Err(fail(e));
    }
    let n = children.finish()?;
    Ok(
        serde_json::json!({"stage":stage,"failure":e.failure,"cleanup_complete":true,"remaining_owned_children":0,"reaped_after_failure":n,"user_exec_released":false}),
    )
}
