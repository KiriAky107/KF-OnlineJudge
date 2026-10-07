//! Fixed Linux isolation operations; launcher owns process/control orchestration.

pub(crate) mod identity;
pub(crate) mod inputs;
pub mod namespaces;
pub(crate) mod proc_dev;
pub(crate) mod root;
pub(crate) mod seccomp;
// No public exec gate until resource supervision and lifecycle acceptance.
#[allow(dead_code)]
pub(crate) mod process_tree;
