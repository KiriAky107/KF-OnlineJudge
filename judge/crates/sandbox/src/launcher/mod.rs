//! Trusted supervisor/launcher coordination, separate from user stdio.
//!
//! Lifecycle events describe completed operations, not commands received from
//! an untrusted request. OS operations and authenticated bounded IPC are pending;
//! this module grants no execution authority and does not change CLI refusal.

pub mod lifecycle;
