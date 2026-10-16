//! Operator-only image routing. All selections retain one global resource pool.
use super::{ConfigError, ImageKind, RuntimeConfig, ValidatedRuntimeConfig, inside, invalid};
use crate::spec::{ExecutionSpec, ResourceLimits};
use kf_contracts::judging_v2::RuntimeIdentity;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RegisteredImage {
    pub image_root: String,
    pub image_kind: ImageKind,
    pub runtime_identity: RuntimeIdentity,
    pub per_task_ceiling: ResourceLimits,
}

fn ceilings(v: &ResourceLimits) -> [u64; 10] {
    [
        v.cpu_time_us,
        v.wall_time_us,
        v.memory_bytes,
        v.stack_bytes,
        u64::from(v.processes),
        u64::from(v.open_files),
        v.stdout_bytes,
        v.stderr_bytes,
        v.file_bytes,
        v.workdir_bytes,
    ]
}
fn selected(config: &RuntimeConfig, image: &RegisteredImage) -> RuntimeConfig {
    let mut c = config.clone();
    c.runtime_images.clear();
    c.image_root = image.image_root.clone();
    c.image_kind = image.image_kind;
    c.runtime_identity = Some(image.runtime_identity.clone());
    c.per_task_ceiling = image.per_task_ceiling.clone();
    c
}
pub(super) fn validate(config: &RuntimeConfig) -> Result<(), ConfigError> {
    if config.runtime_images.len() > 3 {
        return Err(invalid("runtime_images", "registry_too_large"));
    }
    for (i, image) in config.runtime_images.iter().enumerate() {
        if ceilings(&image.per_task_ceiling)
            .into_iter()
            .zip(ceilings(&config.per_task_ceiling))
            .any(|(n, cap)| n > cap)
        {
            return Err(invalid("runtime_images", "ceiling_exceeds_global_budget"));
        }
        // Reuse complete root, identity and aggregate policy validation; clearing
        // the registry makes this nonrecursive and leaves every pool unchanged.
        selected(config, image).validate()?;
        let forbidden = format!("{}/factory", config.state_root);
        if image.image_root == forbidden
            || inside(&image.image_root, &forbidden)
            || inside(&forbidden, &image.image_root)
        {
            return Err(invalid("runtime_images", "factory_storage_overlap"));
        }
        for root in std::iter::once(&config.image_root)
            .chain(config.runtime_images[..i].iter().map(|r| &r.image_root))
        {
            if root == &image.image_root
                || inside(root, &image.image_root)
                || inside(&image.image_root, root)
            {
                return Err(invalid("runtime_images", "image_storage_overlap"));
            }
        }
        if (config.runtime_identity.is_some() && config.image_kind == image.image_kind)
            || config.runtime_images[..i]
                .iter()
                .any(|r| r.image_kind == image.image_kind)
        {
            return Err(invalid("runtime_images", "duplicate_runtime_family"));
        }
    }
    Ok(())
}
impl ValidatedRuntimeConfig {
    /// No host path, config, capacity or identity allocator comes from a request.
    pub fn for_request(&self, request: &ExecutionSpec) -> Result<Self, ConfigError> {
        let c = self.config();
        let selected = if request.runtime_identity == c.runtime_identity
            && c.image_kind.supports(request.profile)
        {
            let mut base = c.clone();
            base.runtime_images.clear();
            base.validate()?
        } else {
            let image = c
                .runtime_images
                .iter()
                .find(|r| {
                    request.runtime_identity.as_ref() == Some(&r.runtime_identity)
                        && r.image_kind.supports(request.profile)
                })
                .ok_or_else(|| invalid("runtime_identity", "unregistered_identity_or_profile"))?;
            selected(c, image).validate()?
        };
        request
            .clone()
            .validate(&selected.spec_policy())
            .map_err(|e| invalid(e.field, "invalid_registered_request"))?;
        Ok(selected)
    }
    pub(crate) fn registered_configs(&self) -> impl Iterator<Item = Self> + '_ {
        self.config().runtime_images.iter().map(|image| {
            selected(self.config(), image)
                .validate()
                .expect("validated image registry")
        })
    }
}
