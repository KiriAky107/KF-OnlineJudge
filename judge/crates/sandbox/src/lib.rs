//! Linux execution boundary: policy, kernel gates, bounded supervision and recovery.
//! Ordinary operator/Worker entries require trusted authority and active isolation.

#[cfg(target_os = "linux")]
pub mod image;
pub mod launcher;
pub mod outcome;
pub mod probe;
pub mod profiles;
pub mod runtime;
pub mod spec;

#[cfg(target_os = "linux")]
pub mod artifacts;

#[cfg(target_os = "linux")]
pub mod cgroup;

#[cfg(target_os = "linux")]
pub mod io;

#[cfg(target_os = "linux")]
pub mod accounting;
#[cfg(target_os = "linux")]
pub mod cleanup;
#[cfg(target_os = "linux")]
pub mod supervisor;

#[cfg(all(target_os = "linux", feature = "vm-acceptance"))]
pub mod acceptance;
#[cfg(target_os = "linux")]
mod isolation;

#[cfg(target_os = "linux")]
pub mod execution;

#[cfg(target_os = "linux")]
pub mod rpc;

#[cfg(target_os = "linux")]
pub mod factory;
#[cfg(target_os = "linux")]
pub mod factory_cycles;
#[cfg(target_os = "linux")]
pub mod factory_preparation;
