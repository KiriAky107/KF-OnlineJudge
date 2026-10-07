//! Supervisor ownership of a leased root and a still-gated namespace target.
use super::blocked::{InitializationFailure, failure};
use super::namespaces::NamespacedTarget;
use crate::isolation::root::RootLease;
use crate::outcome::{ErrorCode, ExecutionStage};
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use std::io;
use std::path::Path;

pub use crate::isolation::root::RootBudget;

pub struct RootedTarget {
    // Terminate the private namespace before deleting its outside directory.
    target: NamespacedTarget,
    lease: RootLease,
    outputs: crate::artifacts::OutputPlan,
}

fn attach_cleanup(error: &mut InitializationFailure, cleanup: Option<io::Error>) {
    if error.cleanup_failure.is_none() {
        error.cleanup_failure = cleanup.as_ref().map(|e| {
            failure(
                ExecutionStage::Cleanup,
                ErrorCode::CleanupIncomplete,
                Some(e),
            )
        });
    }
}

impl RootedTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
    ) -> Result<Self, InitializationFailure> {
        let mut lease =
            RootLease::prepare(config, spec).map_err(|error| InitializationFailure {
                failure: failure(
                    ExecutionStage::RootFilesystem,
                    ErrorCode::OperationFailed,
                    Some(&error.source),
                ),
                cleanup_failure: error.cleanup_error.as_ref().map(|e| {
                    failure(
                        ExecutionStage::Cleanup,
                        ErrorCode::CleanupIncomplete,
                        Some(e),
                    )
                }),
            })?;
        let descriptors = lease.descriptors().map_err(|error| {
            let mut failure = InitializationFailure {
                failure: failure(
                    ExecutionStage::RootFilesystem,
                    ErrorCode::OperationFailed,
                    Some(&error),
                ),
                cleanup_failure: None,
            };
            attach_cleanup(&mut failure, lease.cleanup().err());
            failure
        })?;
        let mut target =
            match NamespacedTarget::prepare_for_root(config, spec, executable, descriptors) {
                Ok(target) => target,
                Err(mut failure) => {
                    attach_cleanup(&mut failure, lease.cleanup().err());
                    return Err(failure);
                }
            };
        if let Err(error) = (|| {
            target.initialize_root(lease.budget)?;
            lease.verify_root(target.init_pid())?;
            lease.verify_root(target.bootstrap_pid())?;
            Ok::<_, io::Error>(())
        })() {
            let mut failure = target
                .prepared
                .refuse_at(ExecutionStage::RootFilesystem, Some(&error));
            attach_cleanup(&mut failure, lease.cleanup().err());
            return Err(failure);
        }
        Ok(Self {
            target,
            lease,
            outputs: crate::artifacts::OutputPlan::for_spec(spec),
        })
    }
    pub fn init_pid(&self) -> u32 {
        self.target.init_pid()
    }
    pub fn bootstrap_pid(&self) -> u32 {
        self.target.bootstrap_pid()
    }
    pub fn cgroup(&self) -> &crate::cgroup::TaskCgroup {
        self.target.cgroup()
    }
    pub fn budget(&self) -> RootBudget {
        self.lease.budget
    }

    /// Snapshot only declared outputs through this held target's verified work
    /// capability. The task is frozen and its PIDs remain owned/unreaped.
    /// Publication and the final post-execution export lifecycle are separate.
    pub fn snapshot_outputs(&self) -> io::Result<Vec<crate::artifacts::OutputSnapshot>> {
        self.cgroup()
            .verify_frozen_members(&[self.bootstrap_pid(), self.init_pid()])?;
        self.lease.verify_root(self.init_pid())?;
        let work = rustix::fs::open(
            format!("/proc/{}/root/work", self.init_pid()),
            rustix::fs::OFlags::PATH | rustix::fs::OFlags::DIRECTORY | rustix::fs::OFlags::CLOEXEC,
            rustix::fs::Mode::empty(),
        )?;
        self.outputs.collect(&work)
    }

    pub(crate) fn initialize_proc_dev(&mut self) -> io::Result<()> {
        self.target.initialize_proc_dev()?;
        self.lease.verify_root(self.init_pid())?;
        self.lease.verify_root(self.bootstrap_pid())?;
        crate::isolation::proc_dev::verify(self.init_pid())
    }

    pub(crate) fn refuse_root_error(mut self, error: &io::Error) -> InitializationFailure {
        let mut failure = self
            .target
            .prepared
            .refuse_at(ExecutionStage::RootFilesystem, Some(error));
        attach_cleanup(&mut failure, self.lease.cleanup().err());
        failure
    }

    /// Restricted proc/dev, identity, capabilities and seccomp are still absent.
    pub fn refuse_remaining_isolation(mut self) -> InitializationFailure {
        let mut failure = self
            .target
            .prepared
            .refuse_at(ExecutionStage::RootFilesystem, None);
        attach_cleanup(&mut failure, self.lease.cleanup().err());
        failure
    }
}
