"""Clocks, clock-offset estimation and latency statistics.

Two clocks, used for two different things, never mixed:

  - **wall** (`time.time_ns()`) is comparable *across* machines only as a
    label, and only if both are synchronised. Every stored event carries
    one so it lines up with the rest of this codebase, where absolute
    Unix timestamps are the single time format (see app/midi.py).
  - **monotonic** (`time.perf_counter_ns()`) is the one to measure
    durations with, but it is meaningful *only on the machine that
    produced it*. Its zero point is arbitrary and differs per process, so
    subtracting one machine's monotonic value from another's is not a
    latency - it is a meaningless number that happens to have units.

That is why:

  - transport RTT is measured entirely on the teacher's own monotonic
    clock (send stamp, ack stamp, subtract), needing no synchronisation;
  - one-way latency is only reported when both clocks are NTP-
    synchronised and the offset uncertainty is recorded with it,
    otherwise `RTT/2` is shown and labelled as a symmetry-based estimate;
  - student reaction time is measured entirely on the student's own
    monotonic clock, from the moment its cue was ready.

ClockOffsetEstimator below is a lightweight four-timestamp exchange, not
NTP, and is deliberately not presented as one - it exists so the symmetry
assumption can be quantified rather than assumed silently.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000


def wall_ns() -> int:
    """Absolute Unix time in nanoseconds - the format everything in this
    codebase stores."""
    return time.time_ns()


def mono_ns() -> int:
    """High-resolution monotonic counter for durations *on this machine*.
    Never transmit this expecting another machine to subtract from it."""
    return time.perf_counter_ns()


def ns_to_ms(value: Optional[float]) -> Optional[float]:
    return None if value is None else value / NS_PER_MS


# ---------------------------------------------------------------------------
# Clock offset estimation
# ---------------------------------------------------------------------------


@dataclass
class OffsetSample:
    """One four-timestamp exchange, the same geometry NTP uses:

        t1 = local  wall time the request left
        t2 = remote wall time it arrived
        t3 = remote wall time the reply left
        t4 = local  wall time the reply arrived
    """

    t1_ns: int
    t2_ns: int
    t3_ns: int
    t4_ns: int

    @property
    def rtt_ns(self) -> int:
        return (self.t4_ns - self.t1_ns) - (self.t3_ns - self.t2_ns)

    @property
    def offset_ns(self) -> float:
        """How far the remote clock is *ahead* of the local one, assuming
        the two directions took equally long. That assumption is exactly
        what makes this an estimate."""
        return ((self.t2_ns - self.t1_ns) + (self.t3_ns - self.t4_ns)) / 2.0


@dataclass
class ClockOffset:
    offset_ns: float
    uncertainty_ns: float
    best_rtt_ns: int
    samples: int
    # True only when the caller has confirmed both hosts are NTP-synced.
    # Nothing here can determine that on its own, which is the point.
    synchronised: bool = False

    def as_dict(self) -> Dict[str, object]:
        return {
            "offset_ns": self.offset_ns,
            "offset_ms": self.offset_ns / NS_PER_MS,
            "uncertainty_ns": self.uncertainty_ns,
            "uncertainty_ms": self.uncertainty_ns / NS_PER_MS,
            "best_rtt_ns": self.best_rtt_ns,
            "samples": self.samples,
            "synchronised": self.synchronised,
            "method": "four-timestamp exchange (not NTP)",
        }


class ClockOffsetEstimator:
    """Collects exchanges and reports the offset implied by the fastest
    ones.

    Low-RTT samples are preferred because a sample's offset error is
    bounded by half its own RTT: a round trip that got stuck in a queue
    tells you almost nothing about the offset, while the quickest ones
    bracket it tightly. The uncertainty reported is half the best RTT -
    the honest bound this method can offer, and typically far worse than
    real NTP."""

    def __init__(self, keep_best: int = 8):
        self.keep_best = max(1, keep_best)
        self._samples: List[OffsetSample] = []

    def add(self, sample: OffsetSample) -> None:
        self._samples.append(sample)

    def add_exchange(self, t1_ns: int, t2_ns: int, t3_ns: int, t4_ns: int) -> None:
        self.add(OffsetSample(t1_ns, t2_ns, t3_ns, t4_ns))

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def estimate(self, synchronised: bool = False) -> Optional[ClockOffset]:
        if not self._samples:
            return None
        best = sorted(self._samples, key=lambda s: s.rtt_ns)[: self.keep_best]
        offsets = [s.offset_ns for s in best]
        offset = statistics.median(offsets)
        best_rtt = best[0].rtt_ns
        spread = (statistics.pstdev(offsets) if len(offsets) >= 2 else 0.0)
        # Half the fastest round trip bounds the asymmetry error; the
        # spread across kept samples covers jitter on top of it.
        uncertainty = max(best_rtt / 2.0, spread)
        return ClockOffset(
            offset_ns=offset,
            uncertainty_ns=uncertainty,
            best_rtt_ns=best_rtt,
            samples=len(self._samples),
            synchronised=synchronised,
        )


# ---------------------------------------------------------------------------
# Latency statistics
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile (q in 0..1) over an already-sortable
    sequence. Nearest-rank rather than interpolation so a reported p99 is
    always an observation that actually happened."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = max(1, math.ceil(q * len(ordered)))
    return float(ordered[min(rank, len(ordered)) - 1])


@dataclass
class LatencyStats:
    """The summary shape the report's benchmark asks for. `unit` is
    carried so a consumer can never mistake nanoseconds for milliseconds."""

    n: int = 0
    lost: int = 0
    loss_rate: Optional[float] = None
    min_ns: Optional[float] = None
    mean_ns: Optional[float] = None
    sd_ns: Optional[float] = None
    median_ns: Optional[float] = None
    p95_ns: Optional[float] = None
    p99_ns: Optional[float] = None
    max_ns: Optional[float] = None
    label: str = ""
    # Set on metrics that are an assumption rather than a measurement, so
    # it travels with the number into every table and plot.
    caveat: Optional[str] = None

    def as_dict(self) -> Dict[str, object]:
        def ms(value: Optional[float]) -> Optional[float]:
            return None if value is None else value / NS_PER_MS

        return {
            "label": self.label,
            "n": self.n,
            "lost": self.lost,
            "loss_rate": self.loss_rate,
            "min_ms": ms(self.min_ns),
            "mean_ms": ms(self.mean_ns),
            "sd_ms": ms(self.sd_ns),
            "median_ms": ms(self.median_ns),
            "p95_ms": ms(self.p95_ns),
            "p99_ms": ms(self.p99_ns),
            "max_ms": ms(self.max_ns),
            "caveat": self.caveat,
        }


def summarize_latency(
    samples: Sequence[float],
    lost: int = 0,
    label: str = "",
    caveat: Optional[str] = None,
) -> LatencyStats:
    """n counts the *successful* samples; loss_rate is over attempts
    (successful + lost), so a run with heavy loss cannot hide it behind a
    flattering mean."""
    values = [float(v) for v in samples if v is not None]
    attempts = len(values) + int(lost)
    stats = LatencyStats(
        n=len(values),
        lost=int(lost),
        loss_rate=(lost / attempts) if attempts else None,
        label=label,
        caveat=caveat,
    )
    if not values:
        return stats
    stats.min_ns = min(values)
    stats.max_ns = max(values)
    stats.mean_ns = statistics.fmean(values)
    stats.sd_ns = statistics.stdev(values) if len(values) >= 2 else 0.0
    stats.median_ns = statistics.median(values)
    stats.p95_ns = percentile(values, 0.95)
    stats.p99_ns = percentile(values, 0.99)
    return stats


def one_way_estimate(
    rtt_samples: Sequence[float],
    clock_offset: Optional[ClockOffset] = None,
    receive_deltas: Optional[Sequence[float]] = None,
) -> LatencyStats:
    """One-way teacher→student latency, reported honestly.

    Two paths, and which one was taken is stated in the result's caveat:

      - both clocks NTP-synchronised (caller asserts it via
        ClockOffset.synchronised) *and* cross-machine wall deltas
        available -> report those, corrected by the measured offset, with
        the offset uncertainty attached;
      - otherwise -> RTT/2, explicitly labelled a symmetry-based estimate,
        because it assumes the two directions are equally fast and they
        often are not.

    The two never silently swap places: read `caveat` before quoting the
    number anywhere."""
    if clock_offset is not None and clock_offset.synchronised and receive_deltas:
        corrected = [float(d) - clock_offset.offset_ns for d in receive_deltas if d is not None]
        return summarize_latency(
            corrected,
            label="one_way_clock_corrected",
            caveat=(
                "cross-machine wall-clock difference corrected by a measured offset; "
                f"offset uncertainty +/-{clock_offset.uncertainty_ns / NS_PER_MS:.2f} ms "
                "(four-timestamp estimate, not NTP-grade)"
            ),
        )
    halved = [float(v) / 2.0 for v in rtt_samples if v is not None]
    return summarize_latency(
        halved,
        label="one_way_symmetry_estimate",
        caveat=(
            "RTT/2 - a symmetry-based ESTIMATE, not a measurement. It assumes the two "
            "directions take equally long. Clocks were not confirmed synchronised, so a "
            "true one-way figure is not available."
        ),
    )


@dataclass
class DispatchTimings:
    """Every timestamp one cue event produced on the student machine.

    Wall values are for lining this event up with the rest of the
    session's records; monotonic values are what durations are computed
    from. The three *_complete/painted fields are software dispatch and
    render moments - the instant a command was written and flushed, or a
    frame was painted. They are NOT the instant an LED emitted light or a
    motor began to move; measuring that needs a photodiode and an
    accelerometer on one acquisition clock (see server/README.md)."""

    teacher_send_wall_ns: Optional[int] = None
    server_receive_wall_ns: Optional[int] = None
    student_receive_wall_ns: Optional[int] = None
    student_receive_monotonic_ns: Optional[int] = None

    queue_enter_monotonic_ns: Optional[int] = None
    cue_dispatch_start_monotonic_ns: Optional[int] = None
    led_command_complete_monotonic_ns: Optional[int] = None
    visual_painted_monotonic_ns: Optional[int] = None
    haptic_command_complete_monotonic_ns: Optional[int] = None
    cue_ready_monotonic_ns: Optional[int] = None
    cue_ready_wall_ns: Optional[int] = None

    student_response_monotonic_ns: Optional[int] = None
    student_response_wall_ns: Optional[int] = None

    def compute_cue_ready(self, monotonic_now: Optional[int] = None, wall_now: Optional[int] = None) -> int:
        """cue_ready = the last of the enabled channels to become ready.

        With guidance mode "both" that is
        max(LED command complete, visual paint complete, haptic serial
        flush complete); with one finger channel it is the later of the
        LED and that channel. The LED key cue is always in the maximum
        because it is present in every mode."""
        candidates = [
            t
            for t in (
                self.led_command_complete_monotonic_ns,
                self.visual_painted_monotonic_ns,
                self.haptic_command_complete_monotonic_ns,
            )
            if t is not None
        ]
        ready = max(candidates) if candidates else (monotonic_now if monotonic_now is not None else mono_ns())
        self.cue_ready_monotonic_ns = ready
        if self.cue_ready_wall_ns is None:
            # Convert through the offset between the two clocks sampled
            # back to back on this machine - never through another host's.
            now_mono = monotonic_now if monotonic_now is not None else mono_ns()
            now_wall = wall_now if wall_now is not None else wall_ns()
            self.cue_ready_wall_ns = now_wall - (now_mono - ready)
        return ready

    @property
    def reaction_time_ns(self) -> Optional[int]:
        """The one reaction-time definition this module uses:

            student_response_monotonic_ns - cue_ready_monotonic_ns

        Both from the student's own monotonic clock. Never teacher_send,
        server_receive or student_receive - those would fold network
        delay and queueing into a measure of a person."""
        if self.student_response_monotonic_ns is None or self.cue_ready_monotonic_ns is None:
            return None
        return self.student_response_monotonic_ns - self.cue_ready_monotonic_ns

    @property
    def queue_wait_ns(self) -> Optional[int]:
        """How long this event sat behind an earlier one. Recorded on its
        own so it can never be mistaken for network delay or for the
        student's reaction."""
        if self.queue_enter_monotonic_ns is None or self.cue_dispatch_start_monotonic_ns is None:
            return None
        return self.cue_dispatch_start_monotonic_ns - self.queue_enter_monotonic_ns

    @property
    def local_dispatch_ns(self) -> Optional[int]:
        """Software dispatch duration: start of cue dispatch to cue ready."""
        if self.cue_dispatch_start_monotonic_ns is None or self.cue_ready_monotonic_ns is None:
            return None
        return self.cue_ready_monotonic_ns - self.cue_dispatch_start_monotonic_ns

    def as_dict(self) -> Dict[str, Optional[int]]:
        data = {
            "teacher_send_wall_ns": self.teacher_send_wall_ns,
            "server_receive_wall_ns": self.server_receive_wall_ns,
            "student_receive_wall_ns": self.student_receive_wall_ns,
            "student_receive_monotonic_ns": self.student_receive_monotonic_ns,
            "queue_enter_monotonic_ns": self.queue_enter_monotonic_ns,
            "cue_dispatch_start_monotonic_ns": self.cue_dispatch_start_monotonic_ns,
            "led_command_complete_monotonic_ns": self.led_command_complete_monotonic_ns,
            "visual_painted_monotonic_ns": self.visual_painted_monotonic_ns,
            "haptic_command_complete_monotonic_ns": self.haptic_command_complete_monotonic_ns,
            "cue_ready_monotonic_ns": self.cue_ready_monotonic_ns,
            "cue_ready_wall_ns": self.cue_ready_wall_ns,
            "student_response_monotonic_ns": self.student_response_monotonic_ns,
            "student_response_wall_ns": self.student_response_wall_ns,
            "reaction_time_ns": self.reaction_time_ns,
            "queue_wait_ns": self.queue_wait_ns,
            "local_dispatch_ns": self.local_dispatch_ns,
        }
        # Named so nobody downstream reads these as physical onset times.
        data["timing_kind"] = "software_dispatch_render"
        return data
