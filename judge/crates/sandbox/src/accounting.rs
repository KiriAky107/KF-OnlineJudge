//! Trusted task-cgroup samples; never infer OOM from a termination signal.
use crate::outcome::{Measurement, MeasurementFailure, MemoryEvents, ResourceUsage};
use std::io;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AccountingSnapshot {
    pub(crate) cpu_us: u64,
    pub(crate) peak: u64,
    pub(crate) events: MemoryEvents,
    pids_max: u64,
}
impl AccountingSnapshot {
    pub fn cpu_time_us(&self) -> u64 {
        self.cpu_us
    }
    pub fn memory_peak_bytes(&self) -> u64 {
        self.peak
    }
    pub fn memory_events(&self) -> &MemoryEvents {
        &self.events
    }
    pub(crate) fn parse(cpu: &str, peak: &str, events: &str, pids: &str) -> io::Result<Self> {
        Ok(Self {
            cpu_us: metric(cpu, "usage_usec")?,
            peak: decimal(peak.trim())?,
            events: MemoryEvents {
                max: metric(events, "max")?,
                oom: metric(events, "oom")?,
                oom_kill: metric(events, "oom_kill")?,
                oom_group_kill: metric(events, "oom_group_kill")?,
            },
            pids_max: metric(pids, "max")?,
        })
    }
    pub(crate) fn monotonic_since(&self, prior: &Self) -> io::Result<()> {
        if self.cpu_us < prior.cpu_us || self.peak < prior.peak {
            return Err(invalid("accounting counter reset"));
        }
        self.event_delta(prior)?;
        self.process_events_since(prior)?;
        Ok(())
    }
    /// Trusted fresh task group's failed creation count. Do not infer a process
    /// limit from EAGAIN alone: it can have unrelated kernel causes.
    pub(crate) fn process_events_since(&self, base: &Self) -> io::Result<u64> {
        self.pids_max
            .checked_sub(base.pids_max)
            .ok_or_else(|| invalid("process event counter reset"))
    }
    fn event_delta(&self, base: &Self) -> io::Result<MemoryEvents> {
        let sub = |a: u64, b: u64| {
            a.checked_sub(b)
                .ok_or_else(|| invalid("accounting counter reset"))
        };
        Ok(MemoryEvents {
            max: sub(self.events.max, base.events.max)?,
            oom: sub(self.events.oom, base.events.oom)?,
            oom_kill: sub(self.events.oom_kill, base.events.oom_kill)?,
            oom_group_kill: sub(self.events.oom_group_kill, base.events.oom_group_kill)?,
        })
    }
    pub(crate) fn usage(&self, base: &Self, wall_us: Option<u64>) -> io::Result<ResourceUsage> {
        self.monotonic_since(base)?;
        Ok(ResourceUsage {
            cpu_time_us: Measurement::Measured {
                value: self.cpu_us - base.cpu_us,
            },
            wall_time_us: wall_us.map_or(
                Measurement::Unavailable {
                    reason: MeasurementFailure::NotStarted,
                },
                |value| Measurement::Measured { value },
            ),
            memory_peak_bytes: Measurement::Measured { value: self.peak },
            memory_events: Measurement::Measured {
                value: self.event_delta(base)?,
            },
        })
    }
}
fn invalid(s: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, s)
}
fn decimal(s: &str) -> io::Result<u64> {
    if s.is_empty() || !s.bytes().all(|b| b.is_ascii_digit()) {
        return Err(invalid("invalid accounting integer"));
    }
    s.parse()
        .map_err(|_| invalid("accounting integer overflow"))
}
fn metric(text: &str, key: &str) -> io::Result<u64> {
    let mut result = None;
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        let name = parts
            .next()
            .ok_or_else(|| invalid("empty accounting entry"))?;
        let value = parts
            .next()
            .ok_or_else(|| invalid("missing accounting value"))?;
        if parts.next().is_some() {
            return Err(invalid("extra accounting column"));
        }
        if name == key {
            if result.is_some() {
                return Err(invalid("duplicate accounting key"));
            }
            result = Some(decimal(value)?);
        }
    }
    result.ok_or_else(|| invalid("missing accounting key"))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn sample(cpu: u64, peak: u64, n: u64) -> AccountingSnapshot {
        AccountingSnapshot {
            cpu_us: cpu,
            peak,
            events: MemoryEvents {
                max: n,
                oom: n,
                oom_kill: n,
                oom_group_kill: n,
            },
            pids_max: n,
        }
    }
    #[test]
    fn counters_use_keys_and_reject_duplicates_missing_overflow_and_malformed_data() {
        let events = "oom_kill 2\nmax 3\nlow 9\noom_group_kill 1\noom 1\n";
        let a = AccountingSnapshot::parse(
            "user_usec 20\nusage_usec 30\nsystem_usec 10\n",
            "4096\n",
            events,
            "max 4\n",
        )
        .unwrap();
        assert_eq!(a.cpu_us, 30);
        assert_eq!(a.events.max, 3);
        for cpu in [
            "usage_usec 1\nusage_usec 2",
            "user_usec 1",
            "usage_usec +1",
            "usage_usec 18446744073709551616",
            "usage_usec 1 extra",
            "usage_usec -1",
        ] {
            assert!(AccountingSnapshot::parse(cpu, "4096", events, "max 0").is_err());
        }
        assert!(AccountingSnapshot::parse("usage_usec 1", "max", events, "max 0").is_err());
        assert!(
            AccountingSnapshot::parse("usage_usec 1", "0", "max 1\noom 1\noom_kill 1", "max 0")
                .is_err()
        );
        for pids in [
            "",
            "other 1",
            "max -1",
            "max 18446744073709551616",
            "max 1\nmax 2",
        ] {
            assert!(AccountingSnapshot::parse("usage_usec 1", "0", events, pids).is_err());
        }
    }
    #[test]
    fn resets_fail_and_usage_keeps_absolute_fresh_group_peak_and_enrollment_cpu() {
        let base = sample(10, 100, 2);
        let last = sample(20, 200, 3);
        let now = sample(40, 300, 4);
        now.monotonic_since(&last).unwrap();
        let u = now.usage(&base, Some(90)).unwrap();
        assert_eq!(u.cpu_time_us, Measurement::Measured { value: 30 });
        assert_eq!(u.memory_peak_bytes, Measurement::Measured { value: 300 });
        assert_eq!(
            u.memory_events,
            Measurement::Measured {
                value: MemoryEvents {
                    max: 2,
                    oom: 2,
                    oom_kill: 2,
                    oom_group_kill: 2
                }
            }
        );
        assert!(sample(19, 300, 4).monotonic_since(&last).is_err());
        assert!(sample(40, 199, 4).monotonic_since(&last).is_err());
        assert!(sample(40, 300, 2).monotonic_since(&last).is_err());
    }
    #[test]
    fn process_events_use_enrollment_delta_and_reject_reset_independently() {
        let base = sample(10, 100, 2);
        let mut current = sample(40, 300, 4);
        assert_eq!(current.process_events_since(&base).unwrap(), 2);
        current.pids_max = 1;
        assert!(current.monotonic_since(&base).is_err());
    }
}
