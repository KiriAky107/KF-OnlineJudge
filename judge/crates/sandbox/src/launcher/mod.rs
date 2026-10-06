//! Trusted supervisor/launcher coordination, separate from user stdio.
//!
//! Lifecycle events describe completed operations, not commands received from
//! an untrusted request. The standalone entry authenticates its parent and uses
//! bounded IPC, but refuses initialization until OS isolation is implemented.

pub mod lifecycle;

#[cfg(target_os = "linux")]
pub mod blocked;
#[cfg(target_os = "linux")]
pub mod entry;
#[cfg(target_os = "linux")]
pub mod protocol;
