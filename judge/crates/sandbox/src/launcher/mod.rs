//! Trusted launcher coordination with per-stage kernel acknowledgement, bounded
//! private control and one final exec gate; user stdio never carries commands.

pub mod lifecycle;

#[cfg(target_os = "linux")]
pub mod blocked;
#[cfg(target_os = "linux")]
pub mod entry;
#[cfg(target_os = "linux")]
pub mod identity;
pub mod inputs;
#[cfg(target_os = "linux")]
pub mod namespaces;
#[cfg(target_os = "linux")]
pub mod proc_dev;
#[cfg(target_os = "linux")]
pub mod protocol;
#[cfg(target_os = "linux")]
pub mod root;
#[cfg(target_os = "linux")]
pub mod seccomp;

#[cfg(target_os = "linux")]
pub(crate) mod execution;
#[cfg(target_os = "linux")]
pub(crate) mod reap;

#[cfg(target_os = "linux")]
pub(crate) mod capture;
#[cfg(all(target_os = "linux", feature = "vm-acceptance"))]
pub(crate) mod rollback;
#[cfg(target_os = "linux")]
pub(crate) mod run;
