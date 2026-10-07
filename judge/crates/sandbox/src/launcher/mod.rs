//! Trusted supervisor/launcher coordination, separate from user stdio.
//!
//! Lifecycle events describe completed operations, not commands received from
//! an untrusted request. The standalone entry authenticates its parent and uses
//! bounded IPC, and refuses user execution. The separate namespace bootstrap initializes
//! only fixed OS isolation operations and remains gated at the missing root stage.

pub mod lifecycle;

#[cfg(target_os = "linux")]
pub mod blocked;
#[cfg(target_os = "linux")]
pub mod entry;
#[cfg(target_os = "linux")]
pub mod namespaces;
#[cfg(target_os = "linux")]
pub mod proc_dev;
#[cfg(target_os = "linux")]
pub mod protocol;
#[cfg(target_os = "linux")]
pub mod root;
