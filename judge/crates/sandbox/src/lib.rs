//! Linux execution boundary. Current implementation only provides read-only observations.
//!
//! JS-01 defines execution contracts; JS-02..04 add the launcher, isolation and limits.
//! Until that work is verified, the CLI cannot execute any user program.

pub mod probe;
pub mod spec;
