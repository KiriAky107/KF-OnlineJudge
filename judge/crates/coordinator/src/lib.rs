//! Internal HTTP process and future lease/state authority.
//!
//! Only process liveness is exposed now. SQLx repositories and task transitions
//! are added after DR-01/PF-01 define their shared schema and concurrency rules.

pub mod api;
