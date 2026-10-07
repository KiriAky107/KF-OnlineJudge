//! Linux execution boundary: policy/contract checks and read-only observations.
//!
//! Execution contracts and lifecycle ordering precede OS launcher/isolation work.
//! Until that work is verified, the CLI cannot execute any user program.

pub mod launcher;
pub mod outcome;
pub mod probe;
pub mod runtime;
pub mod spec;

#[cfg(target_os = "linux")]
pub mod artifacts;

#[cfg(target_os = "linux")]
pub mod cgroup;

#[cfg(target_os = "linux")]
pub mod io;

#[cfg(target_os = "linux")]
mod isolation;
