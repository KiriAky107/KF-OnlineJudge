//! Seccomp initialization checkpoint; the missing user supervisor still refuses exec.
use super::{blocked::InitializationFailure, identity::IdentityReducedTarget};
use crate::{
    artifacts::ArtifactRoot, outcome::ExecutionStage, runtime::ValidatedRuntimeConfig,
    spec::ValidatedSpec,
};
use std::{io, path::Path};

pub struct FilteredTarget {
    target: IdentityReducedTarget,
}
impl FilteredTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
        artifacts: &ArtifactRoot,
    ) -> Result<Self, InitializationFailure> {
        let mut target = IdentityReducedTarget::prepare(config, spec, executable, artifacts)?;
        if let Err(e) = target.initialize_seccomp() {
            return Err(target.refuse_at(ExecutionStage::Seccomp, Some(&e)));
        }
        Ok(Self { target })
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn take_stdin(&mut self) -> Option<crate::artifacts::SnapshotFile> {
        self.target.take_stdin()
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn root_mut(&mut self) -> &mut super::root::RootedTarget {
        self.target.root_mut()
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn finish_resources(&mut self) -> io::Result<()> {
        self.target.finish_resources()
    }
    pub fn root(&self) -> &super::root::RootedTarget {
        self.target.root()
    }
    pub fn identity(&self) -> (u32, u32) {
        self.target.identity()
    }
    pub fn stdin(&self) -> Option<&crate::artifacts::SnapshotFile> {
        self.target.stdin()
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn refuse_at(
        self,
        stage: ExecutionStage,
        error: Option<&io::Error>,
    ) -> InitializationFailure {
        self.target.refuse_at(stage, error)
    }
    pub fn refuse_remaining_isolation(self) -> InitializationFailure {
        self.target.refuse_at(ExecutionStage::Exec, None)
    }
}

pub(crate) fn initialize(
    channel: &mut super::protocol::ControlChannel,
    profile: crate::spec::ExecutionProfile,
) -> io::Result<()> {
    let result = crate::isolation::seccomp::install(profile);
    if let Err(e) = result {
        let _ = channel.send(&super::protocol::NamespaceEvent::Failed {
            errno: e.raw_os_error().and_then(|n| u16::try_from(n).ok()),
        });
        return Err(e);
    }
    channel.send(&super::protocol::NamespaceEvent::SeccompInitialized {})
}
