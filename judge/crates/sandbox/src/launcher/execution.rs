//! Private PID1 runtime protocol: data precedes exec gate, never a shell.
use super::protocol::{ControlChannel, NamespaceCommand, NamespaceEvent};
use crate::{
    io::ChildPipes,
    isolation::process_tree::{ExecObservation, ExecPlan, NamespaceEnd, Reaper},
    outcome::Termination,
    spec::{ExecutionSpec, SpecPolicy},
};
use serde::{Deserialize, Serialize};
use std::{io, time::Duration};
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TreeRecord {
    pub main: Option<Termination>,
    pub exec_errno: Option<u16>,
    pub exec_confirmed: bool,
    pub reaped_descendants: u32,
    pub empty: bool,
}
pub(crate) fn serve(channel: &mut ControlChannel, request: ExecutionSpec) -> io::Result<()> {
    let spec = request
        .validate(&SpecPolicy::default())
        .map_err(|_| io::Error::other("exec request validation"))?;
    channel.execution_deadline(spec.spec().limits.wall_time_us)?;
    let stdio = ChildPipes {
        input: channel.receive_fd()?,
        output: channel.receive_fd()?,
        error: channel.receive_fd()?,
    };
    stdio.validate_for_exec()?;
    let plan = ExecPlan::new(&spec)?;
    channel.send(&NamespaceEvent::ExecutionPrepared {})?;
    // Parent verifies exact frozen group, snapshots/mount budgets and starts
    // its monotonic budget before releasing this final, separately framed gate.
    match channel.receive::<NamespaceCommand>()? {
        NamespaceCommand::ReleaseExecution {} => {}
        NamespaceCommand::Cancel {} => return Ok(()),
        _ => return Err(io::Error::other("exec gate order")),
    }
    let mut reaper = Reaper::spawn(plan, stdio)?;
    channel.send(&NamespaceEvent::ExecutionSpawned {})?;
    channel.send_fd(reaper.main_pidfd())?;
    let mut last = None;
    loop {
        match channel.try_receive::<NamespaceCommand>() {
            Ok(Some(NamespaceCommand::Cancel {})) => reaper.shutdown(NamespaceEnd::Cancelled),
            Ok(Some(_)) => reaper.shutdown(NamespaceEnd::MonitorFailed),
            Err(_) => reaper.shutdown(NamespaceEnd::ControlLost),
            Ok(None) => {}
        }
        let tree = match reaper.poll() {
            Ok(s) => s,
            Err(_) => reaper.shutdown(NamespaceEnd::MonitorFailed),
        };
        let record = TreeRecord {
            main: tree.main,
            exec_errno: match tree.exec {
                ExecObservation::Failed { errno } => Some(errno),
                _ => None,
            },
            exec_confirmed: tree.exec == ExecObservation::NoErrorObserved,
            reaped_descendants: tree.reaped_descendants,
            empty: tree.empty,
        };
        if last.as_ref() != Some(&record) {
            if channel
                .send(&NamespaceEvent::Tree {
                    record: record.clone(),
                })
                .is_err()
            {
                reaper.shutdown(NamespaceEnd::ControlLost);
            }
            last = Some(record);
        }
        if tree.empty {
            reaper.shutdown(NamespaceEnd::Drained);
        }
        std::thread::sleep(Duration::from_millis(2));
    }
}
