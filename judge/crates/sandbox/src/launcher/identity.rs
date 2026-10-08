//! Identity ownership is retained through target teardown, including failed cleanup.
use super::blocked::{InitializationFailure, failure};
use super::inputs::InputBoundTarget;
use crate::artifacts::ArtifactRoot;
use crate::isolation::root::RootError;
use crate::outcome::{ErrorCode, ExecutionStage};
use crate::runtime::ValidatedRuntimeConfig;
use crate::spec::ValidatedSpec;
use rustix::fd::OwnedFd;
use rustix::fs::{AtFlags, Mode, OFlags};
use std::{io, path::Path};

const DIR: OFlags = OFlags::RDONLY
    .union(OFlags::DIRECTORY)
    .union(OFlags::NOFOLLOW)
    .union(OFlags::CLOEXEC);
fn denied(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}
fn private(fd: &OwnedFd) -> io::Result<()> {
    let s = rustix::fs::fstat(fd)?;
    if s.st_uid != 0 || s.st_gid != 0 || s.st_mode & 0o7777 != 0o700 {
        return Err(denied("unsafe identity lease directory"));
    }
    Ok(())
}

struct IdentityLease {
    config: ValidatedRuntimeConfig,
    parent: OwnedFd,
    slot: OwnedFd,
    name: String,
    uid: u32,
    gid: u32,
    removed: bool,
    initialized: bool,
}
impl IdentityLease {
    fn acquire(config: &ValidatedRuntimeConfig, spec: &ValidatedSpec) -> Result<Self, RootError> {
        if !rustix::process::geteuid().is_root() {
            return Err(denied("identity allocator requires privileged supervisor").into());
        }
        let state = crate::isolation::root::open_directory(Path::new(&config.config().state_root))?;
        match rustix::fs::mkdirat(&state, "identities", Mode::from_raw_mode(0o700)) {
            Ok(()) | Err(rustix::io::Errno::EXIST) => {}
            Err(e) => return Err(e.into()),
        }
        let parent = rustix::fs::openat(&state, "identities", DIR, Mode::empty())?;
        private(&parent)?;
        for index in 0..config.config().max_concurrent_runs {
            let (uid, gid) = config
                .task_identity(index)
                .ok_or_else(|| denied("identity slot mapping"))?;
            let name = format!("uid-{uid}");
            match rustix::fs::mkdirat(&parent, name.as_str(), Mode::from_raw_mode(0o700)) {
                Err(rustix::io::Errno::EXIST) => continue, // Never adopt a crash/foreign marker.
                Err(e) => return Err(e.into()),
                Ok(()) => {}
            }
            let slot = match rustix::fs::openat(&parent, name.as_str(), DIR, Mode::empty()) {
                Ok(fd) => fd,
                Err(e) => {
                    return Err(RootError {
                        source: e.into(),
                        cleanup_error: rustix::fs::unlinkat(
                            &parent,
                            name.as_str(),
                            AtFlags::REMOVEDIR,
                        )
                        .err()
                        .map(Into::into),
                    });
                }
            };
            let mut lease = Self {
                config: config.clone(),
                parent,
                slot,
                name,
                uid,
                gid,
                removed: false,
                initialized: false,
            };
            let setup = (|| {
                rustix::fs::fchmod(&lease.slot, Mode::from_raw_mode(0o700))?;
                private(&lease.slot)?;
                crate::cleanup::initialize_slot(config, spec, &lease.slot, uid, gid)?;
                lease.initialized = true;
                rustix::fs::fsync(&lease.slot)?;
                rustix::fs::fsync(&lease.parent)?;
                Ok::<_, io::Error>(())
            })();
            if let Err(source) = setup {
                return Err(RootError {
                    source,
                    cleanup_error: lease.release().err(),
                });
            }
            return Ok(lease);
        }
        Err(rustix::io::Errno::AGAIN.into())
    }
    // There is intentionally no automatic lease release in IdentityLease::Drop.
    // Only a wrapper that has confirmed complete teardown may remove a marker.
    fn release(&mut self) -> io::Result<()> {
        if self.removed {
            return Ok(());
        }
        let named = rustix::fs::openat(&self.parent, self.name.as_str(), DIR, Mode::empty())?;
        let a = rustix::fs::fstat(&named)?;
        let b = rustix::fs::fstat(&self.slot)?;
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino) {
            return Err(denied("identity marker changed"));
        }
        crate::cleanup::verify_release(&self.slot)?;
        if self.initialized {
            crate::cleanup::verify_released_resources(
                &self.config,
                &self.slot,
                self.uid,
                self.gid,
            )?;
        }
        match rustix::fs::unlinkat(&self.slot, ".owner.next", AtFlags::empty()) {
            Ok(()) | Err(rustix::io::Errno::NOENT) => {}
            Err(e) => return Err(e.into()),
        }
        match rustix::fs::unlinkat(&self.slot, "owner.json", AtFlags::empty()) {
            Ok(()) | Err(rustix::io::Errno::NOENT) => {}
            Err(e) => return Err(e.into()),
        }
        rustix::fs::unlinkat(&self.parent, self.name.as_str(), AtFlags::REMOVEDIR)?;
        self.removed = true;
        rustix::fs::fsync(&self.parent)?;
        Ok(())
    }
}
fn release_after_cleanup(lease: &mut IdentityLease, result: &mut InitializationFailure) {
    if result.cleanup_failure.is_none()
        && let Err(e) = lease.release()
    {
        result.cleanup_failure = Some(failure(
            ExecutionStage::Cleanup,
            ErrorCode::CleanupIncomplete,
            Some(&e),
        ));
    }
}

pub struct IdentityReducedTarget {
    target: Option<InputBoundTarget>,
    lease: IdentityLease,
}
impl IdentityReducedTarget {
    pub fn prepare(
        config: &ValidatedRuntimeConfig,
        spec: &ValidatedSpec,
        executable: &Path,
        artifacts: &ArtifactRoot,
    ) -> Result<Self, InitializationFailure> {
        let mut lease =
            IdentityLease::acquire(config, spec).map_err(|e| InitializationFailure {
                failure: failure(
                    ExecutionStage::Identity,
                    ErrorCode::OperationFailed,
                    Some(&e.source),
                ),
                cleanup_failure: e.cleanup_error.as_ref().map(|e| {
                    failure(
                        ExecutionStage::Cleanup,
                        ErrorCode::CleanupIncomplete,
                        Some(e),
                    )
                }),
            })?;
        let mut target = match InputBoundTarget::prepare(config, spec, executable, artifacts) {
            Ok(target) => target,
            Err(mut e) => {
                release_after_cleanup(&mut lease, &mut e);
                return Err(e);
            }
        };
        if let Err(e) = crate::cleanup::verify_owned(config, spec, false)
            .and_then(|()| target.target.root.initialize_rlimits())
        {
            let mut e = target
                .target
                .root
                .refuse_stage(ExecutionStage::Preparation, Some(&e));
            release_after_cleanup(&mut lease, &mut e);
            return Err(e);
        }
        if let Err(e) = target.target.root.initialize_identity(lease.uid, lease.gid) {
            let mut e = target
                .target
                .root
                .refuse_stage(ExecutionStage::Identity, Some(&e));
            release_after_cleanup(&mut lease, &mut e);
            return Err(e);
        }
        Ok(Self {
            target: Some(target),
            lease,
        })
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn take_stdin(&mut self) -> Option<crate::artifacts::SnapshotFile> {
        self.target.as_mut().expect("owned target").take_stdin()
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn root_mut(&mut self) -> &mut super::root::RootedTarget {
        self.target.as_mut().expect("owned target").root_mut()
    }
    #[cfg(feature = "vm-acceptance")]
    pub(crate) fn finish_resources(&mut self) -> io::Result<()> {
        self.root_mut().finish_resources()?;
        self.lease.release()
    }
    pub fn root(&self) -> &super::root::RootedTarget {
        self.target.as_ref().expect("owned target").root()
    }
    pub fn identity(&self) -> (u32, u32) {
        (self.lease.uid, self.lease.gid)
    }
    pub fn stdin(&self) -> Option<&crate::artifacts::SnapshotFile> {
        self.target.as_ref().expect("owned target").stdin()
    }
    pub(crate) fn initialize_seccomp(&mut self) -> io::Result<()> {
        self.target
            .as_mut()
            .expect("owned target")
            .target
            .root
            .initialize_seccomp()
    }
    pub(crate) fn refuse_at(
        mut self,
        stage: ExecutionStage,
        error: Option<&io::Error>,
    ) -> InitializationFailure {
        let target = self.target.take().expect("owned target");
        let mut result = target.target.root.refuse_stage(stage, error);
        release_after_cleanup(&mut self.lease, &mut result);
        result
    }
    pub fn refuse_remaining_isolation(self) -> InitializationFailure {
        self.refuse_at(ExecutionStage::Seccomp, None)
    }
}
impl Drop for IdentityReducedTarget {
    fn drop(&mut self) {
        if let Some(target) = self.target.take() {
            let mut result = target
                .target
                .root
                .refuse_stage(ExecutionStage::Seccomp, None);
            release_after_cleanup(&mut self.lease, &mut result);
            // An error leaves the durable marker reserved for restart recovery.
        }
    }
}

pub(crate) fn initialize(
    channel: &mut super::protocol::ControlChannel,
    uid: u32,
    gid: u32,
) -> io::Result<()> {
    let result = crate::isolation::identity::reduce_init(uid, gid);
    if let Err(e) = &result {
        let _ = channel.send(&super::protocol::NamespaceEvent::Failed {
            errno: e.raw_os_error().and_then(|n| u16::try_from(n).ok()),
        });
        return result;
    }
    channel.send(&super::protocol::NamespaceEvent::IdentityInitialized {})
}
