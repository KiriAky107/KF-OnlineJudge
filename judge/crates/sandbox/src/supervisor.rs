//! Aggregate task budgets and monotonic exec wall clock, not a public exec gate.
//! Caller owns/reaps the bootstrap and removes roots only after this bounded
//! termination/final-accounting step. No CPU.max-to-total-time approximation.
use crate::{
    accounting::AccountingSnapshot,
    cgroup::TaskCgroup,
    outcome::{LimitEvidence, LimitObservation, Measurement, MeasurementFailure, ResourceUsage},
    spec::ValidatedSpec,
};
use std::{
    io,
    time::{Duration, Instant},
};

const TERMINATE: Duration = Duration::from_secs(2);
#[derive(Debug)]
pub enum BudgetEvent {
    Running {
        usage: ResourceUsage,
    },
    Finished {
        usage: ResourceUsage,
        observations: Vec<LimitObservation>,
    },
}
#[derive(Debug)]
pub struct SupervisionFailure {
    pub source: io::Error,
    pub termination_error: Option<io::Error>,
    pub usage: ResourceUsage,
}
pub struct BudgetSupervisor<'a> {
    group: &'a mut TaskCgroup,
    base: AccountingSnapshot,
    last: AccountingSnapshot,
    cpu_limit: u64,
    wall_limit: u64,
    started: Option<Instant>,
    finished_at: Option<Instant>,
    observations: Vec<LimitObservation>,
    ended: bool,
}
impl<'a> BudgetSupervisor<'a> {
    pub fn prepare(
        group: &'a mut TaskCgroup,
        spec: &ValidatedSpec,
    ) -> Result<Self, SupervisionFailure> {
        let read = (|| {
            if group.name() != format!("kf-{}", spec.spec().run_id) {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "budget task identity",
                ));
            }
            let base = group.accounting_baseline()?.clone();
            let last = group.accounting_snapshot()?;
            last.monotonic_since(&base)?;
            Ok((base, last))
        })();
        match read {
            Ok((base, last)) => {
                let mut supervisor = Self {
                    group,
                    base,
                    last,
                    cpu_limit: spec.spec().limits.cpu_time_us,
                    wall_limit: spec.spec().limits.wall_time_us,
                    started: None,
                    finished_at: None,
                    observations: Vec::with_capacity(3),
                    ended: false,
                };
                if supervisor.last.cpu_time_us() - supervisor.base.cpu_time_us()
                    >= supervisor.cpu_limit
                {
                    return Err(supervisor.exhausted());
                }
                Ok(supervisor)
            }
            Err(source) => Err(SupervisionFailure {
                termination_error: group.terminate_tree(TERMINATE).err(),
                usage: unavailable(&source, None),
                source,
            }),
        }
    }
    /// Timestamp immediately before releasing the actual kernel exec gate.
    /// This records time only: it neither thaws a task nor grants exec authority.
    pub fn mark_exec_release(&mut self) -> Result<(), SupervisionFailure> {
        if self.started.is_some() || self.ended {
            return Err(self.fail(io::Error::new(
                io::ErrorKind::InvalidInput,
                "exec clock state",
            )));
        }
        match self.group.accounting_snapshot().and_then(|s| {
            s.monotonic_since(&self.last)?;
            Ok(s)
        }) {
            Ok(s) if s.cpu_time_us() - self.base.cpu_time_us() < self.cpu_limit => {
                self.last = s;
                self.started = Some(Instant::now());
                Ok(())
            }
            Ok(s) => {
                self.last = s;
                Err(self.exhausted())
            }
            Err(e) => Err(self.fail(e)),
        }
    }
    pub fn poll(&mut self) -> Result<BudgetEvent, SupervisionFailure> {
        if self.started.is_none() || self.ended {
            return Err(self.fail(io::Error::new(
                io::ErrorKind::InvalidInput,
                "budget poll state",
            )));
        }
        let result = (|| {
            let sample = self.group.accounting_snapshot()?;
            sample.monotonic_since(&self.last)?;
            let usage = sample.usage(&self.base, self.wall_us()?)?;
            self.observe(&usage);
            self.last = sample;
            if !self.observations.is_empty() {
                self.group.terminate_tree(TERMINATE)?;
                return self.finish_sample();
            }
            if self.group.is_empty()? {
                return self.finish_sample();
            }
            Ok(BudgetEvent::Running { usage })
        })();
        result.map_err(|e| self.fail(e))
    }
    /// Explicit cancellation/other monitor faults use the same tree shutdown.
    /// Cause classification remains with the caller's cancellation/error record.
    pub fn terminate(&mut self) -> Result<BudgetEvent, SupervisionFailure> {
        let result = self
            .group
            .terminate_tree(TERMINATE)
            .and_then(|()| self.finish_sample());
        result.map_err(|e| self.fail(e))
    }
    fn finish_sample(&mut self) -> io::Result<BudgetEvent> {
        if !self.group.is_empty()? {
            return Err(io::Error::other("tree not empty before final accounting"));
        }
        self.finished_at.get_or_insert_with(Instant::now);
        let sample = self.group.accounting_snapshot()?;
        sample.monotonic_since(&self.last)?;
        let usage = sample.usage(&self.base, self.wall_us()?)?;
        self.observe(&usage);
        self.last = sample;
        self.ended = true;
        Ok(BudgetEvent::Finished {
            usage,
            observations: self.observations.clone(),
        })
    }
    fn wall_us(&self) -> io::Result<Option<u64>> {
        self.started
            .map(|start| {
                u64::try_from(
                    self.finished_at
                        .unwrap_or_else(Instant::now)
                        .duration_since(start)
                        .as_micros(),
                )
                .map_err(|_| io::Error::other("wall counter overflow"))
            })
            .transpose()
    }
    fn observe(&mut self, usage: &ResourceUsage) {
        let Measurement::Measured { value: wall } = usage.wall_time_us else {
            return;
        };
        let mut found = Vec::with_capacity(3);
        if matches!(&usage.memory_events,Measurement::Measured {value:e} if e.max>0&&e.oom>0&&e.oom_kill>0&&e.oom_group_kill>0)
        {
            found.push(LimitEvidence::Memory);
        }
        if let Measurement::Measured { value } = usage.cpu_time_us
            && value >= self.cpu_limit
        {
            found.push(LimitEvidence::CpuTime { observed_us: value });
        }
        if wall >= self.wall_limit {
            found.push(LimitEvidence::WallTime { observed_us: wall });
        }
        for evidence in found {
            if !self
                .observations
                .iter()
                .any(|o| o.evidence.kind() == evidence.kind())
            {
                self.observations.push(LimitObservation {
                    at_wall_time_us: wall,
                    evidence,
                });
            }
        }
    }
    fn fail(&mut self, source: io::Error) -> SupervisionFailure {
        let termination_error = self.group.terminate_tree(TERMINATE).err();
        self.ended = termination_error.is_none();
        if self.ended {
            self.finished_at.get_or_insert_with(Instant::now);
        }
        let wall = self.wall_us().ok().flatten();
        SupervisionFailure {
            usage: unavailable(&source, wall),
            source,
            termination_error,
        }
    }
    fn exhausted(&mut self) -> SupervisionFailure {
        let source = io::Error::new(
            io::ErrorKind::TimedOut,
            "initialization consumed CPU budget",
        );
        let termination_error = self.group.terminate_tree(TERMINATE).err();
        self.ended = termination_error.is_none();
        // A verified budget exhaustion is not a failed read. Preserve actual
        // final counters; the exec wall clock correctly remains NotStarted.
        let usage = match self.group.accounting_snapshot().and_then(|s| {
            s.monotonic_since(&self.last)?;
            s.usage(&self.base, None)
        }) {
            Ok(usage) => usage,
            Err(e) => unavailable(&e, None),
        };
        SupervisionFailure {
            source,
            termination_error,
            usage,
        }
    }
}
impl Drop for BudgetSupervisor<'_> {
    fn drop(&mut self) {
        if !self.ended {
            let _ = self.group.terminate_tree(TERMINATE);
        }
    }
}
fn unavailable(source: &io::Error, wall: Option<u64>) -> ResourceUsage {
    let reason = if source.kind() == io::ErrorKind::InvalidData {
        MeasurementFailure::InvalidData
    } else {
        MeasurementFailure::ReadFailed
    };
    ResourceUsage {
        cpu_time_us: Measurement::Unavailable { reason },
        wall_time_us: wall.map_or(
            Measurement::Unavailable {
                reason: MeasurementFailure::NotStarted,
            },
            |value| Measurement::Measured { value },
        ),
        memory_peak_bytes: Measurement::Unavailable { reason },
        memory_events: Measurement::Unavailable { reason },
    }
}
