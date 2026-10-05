//! Reserved for deterministic judging orchestration (JS-05/JS-06).
//!
//! Owns language adaptation, case order, output comparison and verdict aggregation.
//! Execution is supplied through a port; this crate must not access SQL, AMQP,
//! HTTP, privilege setup or hidden host paths directly.
//! No fake verdict or user-program execution is provided by this scaffold.
