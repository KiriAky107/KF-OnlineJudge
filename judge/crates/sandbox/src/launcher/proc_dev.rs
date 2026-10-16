//! A separately gated restricted-filesystem stage, with no user execution API.
use super::blocked::InitializationFailure;
use super::root::RootedTarget;
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use std::path::Path;

pub struct RestrictedRootTarget {
    pub(crate) root: RootedTarget,
}

impl RestrictedRootTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
    ) -> Result<Self, InitializationFailure> {
        let mut root = RootedTarget::prepare(config, spec, executable)?;
        if let Err(error) = root.initialize_proc_dev() {
            return Err(root.refuse_root_error(&error));
        }
        Ok(Self { root })
    }
    pub fn root(&self) -> &RootedTarget {
        &self.root
    }
    /// Input binding, identity/capabilities/seccomp and supervision are absent.
    pub fn refuse_remaining_isolation(self) -> InitializationFailure {
        self.root.refuse_remaining_isolation()
    }
}
