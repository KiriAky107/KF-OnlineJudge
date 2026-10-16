//! Authenticated descriptor transfer and fixed pre-exec input initialization.
use super::blocked::{InitializationFailure, failure};
use super::proc_dev::RestrictedRootTarget;
use super::protocol::{ControlChannel, NamespaceCommand, NamespaceEvent};
use crate::artifacts::{ArtifactRoot, SnapshotFile};
use crate::outcome::{ErrorCode, ExecutionStage};
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use std::{io, path::Path};

pub struct InputBoundTarget {
    pub(crate) target: RestrictedRootTarget,
    stdin: Option<SnapshotFile>,
}

impl InputBoundTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
        artifacts: &ArtifactRoot,
    ) -> Result<Self, InitializationFailure> {
        // Resolve/copy before starting the bounded helper handshake.
        let inputs = artifacts
            .snapshot_inputs(spec)
            .map_err(|error| InitializationFailure {
                failure: failure(
                    ExecutionStage::Preparation,
                    ErrorCode::OperationFailed,
                    Some(&error),
                ),
                cleanup_failure: None,
            })?;
        let target = RestrictedRootTarget::prepare(config, spec, executable)?;
        Self::bind_prepared(target, inputs)
    }

    /// Resume an existing verified restricted root. Artifacts are always
    /// resolved against the immutable request saved with that same target.
    pub fn bind(
        target: RestrictedRootTarget,
        artifacts: &ArtifactRoot,
    ) -> Result<Self, InitializationFailure> {
        let inputs = match artifacts.snapshot_inputs(&target.root.request) {
            Ok(inputs) => inputs,
            Err(error) => return Err(target.root.refuse_root_error(&error)),
        };
        Self::bind_prepared(target, inputs)
    }

    fn bind_prepared(
        mut target: RestrictedRootTarget,
        inputs: crate::artifacts::PreparedInputs,
    ) -> Result<Self, InitializationFailure> {
        if let Err(error) = target.root.initialize_inputs(&inputs) {
            return Err(target.root.refuse_root_error(&error));
        }
        // Only the independent stdin handle remains in the supervisor. Work
        // input FDs are dropped after verifying the actual bound content and mounts.
        Ok(Self {
            target,
            stdin: inputs.stdin,
        })
    }
    pub(crate) fn take_stdin(&mut self) -> Option<SnapshotFile> {
        self.stdin.take()
    }
    pub(crate) fn root_mut(&mut self) -> &mut super::root::RootedTarget {
        &mut self.target.root
    }
    pub fn root(&self) -> &super::root::RootedTarget {
        self.target.root()
    }
    pub fn stdin(&self) -> Option<&SnapshotFile> {
        self.stdin.as_ref()
    }
    pub fn refuse_remaining_isolation(self) -> InitializationFailure {
        self.target.refuse_remaining_isolation()
    }
}

pub(crate) fn initialize(channel: &mut ControlChannel, count: u32, bytes: u64) -> io::Result<()> {
    let setup = (|| {
        channel.send(&NamespaceEvent::InputsStarted {})?;
        let mut total = 0_u64;
        let mut paths = std::collections::BTreeSet::new();
        for _ in 0..count {
            let NamespaceCommand::AttachInput {
                path,
                bytes: size,
                executable,
            } = channel.receive()?
            else {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "input stage order",
                ));
            };
            total = total
                .checked_add(size)
                .filter(|total| *total <= bytes)
                .ok_or_else(|| io::Error::new(io::ErrorKind::PermissionDenied, "input total"))?;
            if !paths.insert(path.clone()) {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "duplicate input",
                ));
            }
            let fd = channel.receive_fd()?;
            crate::isolation::inputs::bind(&path, &fd, size, executable)?;
            drop(fd);
            channel.send(&NamespaceEvent::InputAttached {})?;
        }
        if total != bytes
            || channel.receive::<NamespaceCommand>()? != (NamespaceCommand::FinishInputs {})
        {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "unfinished input batch",
            ));
        }
        crate::isolation::inputs::finish()?;
        channel.send(&NamespaceEvent::InputsInitialized {})
    })();
    if let Err(error) = &setup {
        let _ = channel.send(&NamespaceEvent::Failed {
            errno: error.raw_os_error().and_then(|n| u16::try_from(n).ok()),
        });
    }
    setup
}
