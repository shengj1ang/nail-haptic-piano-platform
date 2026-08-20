"""Transport latency benchmark for the relayed teacher -> student path.

Defaults follow the report's `System Transmission Performance
Benchmarking`: 1000 simulated key/finger commands at 500 ms intervals
over one persistent WebSocket per endpoint, with warm-up samples recorded
separately and excluded from the statistics. Every probe carries a unique
sequence number and message id.

What is measured, and why it is split up
----------------------------------------
- **Transport RTT** (primary). `perf_counter_ns()` on this machine when
  the probe goes out, the same clock when `latency.received` comes back,
  subtract. One clock, one machine - no synchronisation needed, which is
  exactly why this is the headline figure.
- **Teacher -> server ack**: probe out to the relay's `latency.ack`.
  Isolates which hop is slow when a run looks bad.
- **Cue-presented RTT**: probe out to `latency.presented`, i.e. including
  the student's local cue dispatch.
- **Student local dispatch**: measured entirely on the student's own
  monotonic clock and reported back as a duration. Never subtracted from
  anything of ours.
- **One-way**: reported as a clock-corrected measurement only when the
  operator confirms both hosts are NTP-synchronised (`--clocks-synced`)
  *and* the offset estimate is recorded with it. Otherwise RTT/2, labelled
  in the output as a symmetry-based estimate. See timing.one_way_estimate.

What is deliberately not claimed
--------------------------------
Nothing here is a physical cue onset. `latency.presented` marks the
moment the student's software finished dispatching - the serial write
flushed, the frame painted. Light actually emitted and motor actually
moving lag that, and measuring them needs a photodiode on the key and an
accelerometer on the actuator sampled on one acquisition clock. The CSV
keeps columns free for those; this tool does not fill them in.

Rooms are never the operator's job. Every run creates its own temporary
benchmark room as the teacher, puts both roles in it, reports the
membership the relay confirms, and closes (never deletes) that room
afterwards. By default the tool is fully self-contained: it logs in once
as a teacher and once as a student, joins both to the new room, opens
both WebSockets and makes the built-in student endpoint answer probes -
only the relay and this process need to be running. ``--external-student``
swaps the built-in responder for the real Student Client; the room is
still created here and its join code printed for that client to use, and
probing waits until the relay says a student is actually in the room.
``--room-id`` is the one manual escape hatch, for an external Student
Client that has already joined a room of its own.

Usage from main/:

    python remote_latency_benchmark.py --server http://127.0.0.1:18765 \\
        --username teacher1 --student-username student1

The built-in student measures the network/relay path but deliberately
does not pretend to be the real Student UI, LED or haptic hardware.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import math
import random
import re
import signal
import statistics
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import RemoteGuidanceConfig
from ..network_client import ClientCallbacks, RemoteApiClient, RemoteApiError, RemoteWebSocketClient
from ..protocol import (
    TYPE_LATENCY_ACK,
    TYPE_LATENCY_PRESENTED,
    TYPE_LATENCY_PROBE,
    TYPE_LATENCY_RECEIVED,
    TYPE_PRESENCE,
)
from ..timing import (
    ClockOffsetEstimator,
    LatencyStats,
    mono_ns,
    one_way_estimate,
    summarize_latency,
    wall_ns,
)

log = logging.getLogger("remote_guidance.benchmark")

DEFAULT_COUNT = 1000
DEFAULT_INTERVAL_S = 0.5
DEFAULT_WARMUP = 20
DEFAULT_TIMEOUT_S = 5.0

# Probe pacing. A fixed period is easy to reason about but samples the
# same phase of anything periodic in the path - relay heartbeats, Wi-Fi
# power-save windows, scheduler ticks - on every probe, which biases the
# result in whichever direction that phase happens to sit. Randomised
# pacing is the standard answer (RFC 2330 § 11.1): `poisson` draws each
# wait from an exponential distribution, which is its unbiased choice;
# `uniform` jitters around the mean instead, keeping the run's length
# predictable. Every mode has the same mean, so the probe rate and the
# run duration are unchanged by the choice.
INTERVAL_MODES = ("fixed", "uniform", "poisson", "human")
DEFAULT_INTERVAL_MODE = "fixed"
DEFAULT_INTERVAL_JITTER = 0.5
# An exponential wait is unbounded, and one unlucky draw should not stall
# a run; RFC 2330 truncates the tail for the same reason. At 5x the mean
# this discards under 1% of the distribution.
POISSON_MAX_INTERVAL_FACTOR = 5.0

# `human` paces probes the way this platform is actually driven: a cue
# goes out, a person reacts, the next one follows. Human reaction time is
# neither periodic nor exponential - it clusters around a typical value
# and is skewed right by the slow trials - which is the lognormal shape.
#
# Its two parameters are fitted to this study's own quiz data at run time
# (see measure_human_pacing) rather than written down here on purpose:
# the participant set grows, and a constant fitted to P01-P14 would go on
# describing 14 people long after P15 and P20 were recorded, without ever
# looking wrong.
#
# The values below are only the fallback for a machine with no quiz data
# at all - a fresh checkout, a copied-out deployment - and are the fit as
# it stood at 14 participants and 11,335 trials.
FALLBACK_HUMAN_MEDIAN_S = 0.672
FALLBACK_HUMAN_LOG_SIGMA = 0.441
# Under this many trials a fit describes the sample rather than human
# reaction time, so the fallback is the more honest answer.
MIN_HUMAN_TRIALS = 100
# Resamples behind the reported 95% interval on the pooled parameters.
BOOTSTRAP_RESAMPLES = 2000
# 10x the median rather than poisson's 5x: at this sigma that is over
# five standard deviations up in log space, so it truncates nothing the
# model really produces while still bounding a stalled run.
HUMAN_MAX_INTERVAL_FACTOR = 10.0

# How long an external-student run waits for the real Student Client to
# join the room this tool just created. Long enough to walk to the other
# machine and type the join code, short enough that an unattended run
# fails rather than hanging forever.
DEFAULT_STUDENT_WAIT_S = 180.0

# Prefix for the room-lifecycle lines. benchmark_window.py reads these
# back out of the child's stdout to show the room it created, so the
# wording after the prefix is part of that (very small) contract.
ROOM_LINE = "room:"

RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "remote_guidance" / "latency"

CSV_COLUMNS = [
    "seq",
    "probe_id",
    "warmup",
    "sent_wall_ns",
    "rtt_ns",
    "server_ack_rtt_ns",
    "presented_rtt_ns",
    "student_dispatch_ns",
    "student_receive_wall_ns",
    "server_receive_wall_ns",
    "wall_delta_teacher_to_student_ns",
    "lost",
    # Reserved for a future photodiode/accelerometer rig on one
    # acquisition clock. This tool never fills them - see the module
    # docstring on why software dispatch is not physical onset.
    "physical_led_onset_ns",
    "physical_haptic_onset_ns",
]


@dataclass
class ProbeSample:
    seq: int
    probe_id: str
    warmup: bool = False
    sent_mono_ns: int = 0
    sent_wall_ns: int = 0
    rtt_ns: Optional[int] = None
    server_ack_rtt_ns: Optional[int] = None
    presented_rtt_ns: Optional[int] = None
    student_dispatch_ns: Optional[int] = None
    student_receive_wall_ns: Optional[int] = None
    server_receive_wall_ns: Optional[int] = None

    @property
    def lost(self) -> bool:
        """A probe is lost when its immediate ack never arrived. The
        cue-presented reply is a separate, optional stage - a probe that
        was received but whose cue reply was late is not transport loss."""
        return self.rtt_ns is None

    @property
    def wall_delta_ns(self) -> Optional[int]:
        """Student receive wall clock minus teacher send wall clock.

        Only a latency if the two machines' clocks agree; kept so
        one_way_estimate() can correct it when the operator has confirmed
        synchronisation, and never reported raw as a one-way figure."""
        if self.student_receive_wall_ns is None:
            return None
        return self.student_receive_wall_ns - self.sent_wall_ns

    def as_row(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "probe_id": self.probe_id,
            "warmup": int(self.warmup),
            "sent_wall_ns": self.sent_wall_ns,
            "rtt_ns": self.rtt_ns,
            "server_ack_rtt_ns": self.server_ack_rtt_ns,
            "presented_rtt_ns": self.presented_rtt_ns,
            "student_dispatch_ns": self.student_dispatch_ns,
            "student_receive_wall_ns": self.student_receive_wall_ns,
            "server_receive_wall_ns": self.server_receive_wall_ns,
            "wall_delta_teacher_to_student_ns": self.wall_delta_ns,
            "lost": int(self.lost),
            "physical_led_onset_ns": None,
            "physical_haptic_onset_ns": None,
        }


@dataclass
class BenchmarkConfig:
    count: int = DEFAULT_COUNT
    interval_s: float = DEFAULT_INTERVAL_S
    warmup: int = DEFAULT_WARMUP
    ack_timeout_s: float = DEFAULT_TIMEOUT_S
    trigger_cue: bool = False
    clocks_synced: bool = False
    built_in_student: bool = False
    note: int = 60
    finger: str = "R1"
    # interval_s is the *mean* wait once pacing is randomised.
    interval_mode: str = DEFAULT_INTERVAL_MODE
    interval_jitter: float = DEFAULT_INTERVAL_JITTER
    # `human` mode only: the log-space spread of its lognormal. None
    # means "fit it to the current participants" - see human_pacing_for.
    human_log_sigma: Optional[float] = None
    # Filled in by LatencyBenchmark when it is None, so summary.json
    # always records the seed a run actually paced itself with and that
    # run can be repeated exactly.
    interval_seed: Optional[int] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParticipantPacing:
    """One participant's own reaction-time distribution."""

    participant: str
    median_s: float
    log_sigma: float
    trials: int
    ks: Optional[float] = None  # fit quality, when scipy is available

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HumanPacing:
    """The model `human` mode draws its waits from, and its evidence.

    Provenance travels with the numbers because a run paced this way is
    only interpretable if the population behind it is known - "measured
    from 14 participants" and "from 20" are different claims - and
    because a dissertation should be able to quote the fit's quality,
    not just its parameters."""

    median_s: float
    log_sigma: float
    participants: List[ParticipantPacing] = field(default_factory=list)
    trials: int = 0
    # Three distinct things, and collapsing any two would misreport a
    # run: fitted to the study's data, fitted to nothing because there
    # was none, or simply typed in by the operator.
    source: str = "fallback fit - no quiz data found"
    # Candidate distribution families with their KS statistic and AIC,
    # so "why lognormal" has an answer in the run's own output.
    families: Dict[str, Dict[str, float]] = field(default_factory=dict)
    median_ci: Optional[Tuple[float, float]] = None
    log_sigma_ci: Optional[Tuple[float, float]] = None
    analysis_s: float = 0.0

    @property
    def participant_count(self) -> int:
        return len(self.participants)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "median_s": self.median_s,
            "log_sigma": self.log_sigma,
            "participant_count": self.participant_count,
            "participants": [p.as_dict() for p in self.participants],
            "trials": self.trials,
            "source": self.source,
            "families": self.families,
            "median_ci": list(self.median_ci) if self.median_ci else None,
            "log_sigma_ci": list(self.log_sigma_ci) if self.log_sigma_ci else None,
            "analysis_s": self.analysis_s,
        }


# The fit is cached against a fingerprint of the quiz folder rather than
# cleared by hand, so recording a participant invalidates it by itself.
_PACING_CACHE: Dict[Tuple[str, str], HumanPacing] = {}


def _quiz_fingerprint(results_files: List[Path]) -> str:
    """Cheap "has anything changed" key: how many results.json there are
    and each one's size and mtime. Stat calls only - it must stay much
    cheaper than the analysis it guards."""
    parts = []
    for path in results_files:
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return f"{len(parts)}|" + str(hash("|".join(parts)))


def _quiz_results_files(quiz_dir: Path) -> List[Tuple[str, Path]]:
    """(participant, results.json) for every trial folder, in order.

    TEST-* and remote-* folders are not participants and are skipped by
    the pattern; the participant number is not assumed to stop at 14."""
    if not quiz_dir.is_dir():
        return []
    found = []
    for directory in sorted(quiz_dir.glob("*")):
        match = re.match(r"(P\d+)-T\d+", directory.name)
        if match is None:
            continue
        results = directory / "results.json"
        if results.is_file():
            found.append((match.group(1), results))
    return found


def clear_pacing_cache() -> None:
    _PACING_CACHE.clear()


def cached_human_pacing(quiz_dir: Optional[Path] = None) -> Optional[HumanPacing]:
    """The fit if it is already computed, without computing it.

    Lets a GUI decide to show a progress dialog instead of freezing on
    the analysis, which is what a window must do even when the analysis
    is fast."""
    from app.quiz import QUIZ_DATA_DIR

    directory = Path(QUIZ_DATA_DIR if quiz_dir is None else quiz_dir)
    files = [path for _, path in _quiz_results_files(directory)]
    return _PACING_CACHE.get((str(directory), _quiz_fingerprint(files)))


def measure_human_pacing(
    quiz_dir: Optional[Path] = None,
    progress: Optional[Any] = None,
    force: bool = False,
) -> HumanPacing:
    """Fit human pacing to every trial of whichever participants exist.

    Reuses `app.quiz`'s loader, validity rule and reaction-time field, so
    this cannot drift from what the study's own analysis counts: a trial
    is usable when it neither timed out nor was manually invalidated as
    carry-over. Non-positive reaction times - the press-before-cue
    artefact - have no logarithm and are dropped.

    Deliberately *not* a constant fitted once and written down: the
    participant set grows, and P01-P14's numbers would go on describing
    14 people long after P20 was recorded, without ever looking wrong.

    Three things beyond the pooled fit, because "all the data" should
    mean the analysis as well as the sample:

    - **per participant.** Pooling every trial into one distribution
      mixes within-person variability with between-person differences
      and inflates sigma (0.441 pooled against 0.424 averaged over
      participants, on P01-P14). `human` mode therefore samples
      hierarchically - a participant, then that participant's own
      lognormal - which reproduces both spreads instead of one blurred
      middle.
    - **which family.** Lognormal is not assumed: it is fitted against
      ex-Gaussian and shifted lognormal and wins on this data
      (KS 0.029 against 0.062; AIC lower by 112). The comparison is
      recorded so the choice can be defended, and re-run as data arrives.
    - **how certain.** A bootstrap over the pooled log-RTs gives a 95%
      interval for the median and sigma.

    scipy is optional: without it the pooled and per-participant fits
    still happen, and the family comparison and interval are skipped.

    `progress(done, total, message)` is called as it goes, for a caller
    that wants to show a progress bar. Results are cached against a
    fingerprint of the quiz folder, so this recomputes when a
    participant is added and not otherwise; `force` recomputes anyway.

    `app.quiz` is imported inside this function on purpose: it reaches
    the finger-matching stack and so numpy, and this module is otherwise
    free of that weight - a benchmark process paces a precise loop, and
    a copied-out relay has no camera code in it at all."""
    from app.quiz import QUIZ_DATA_DIR, VALIDITY_INVALID_CARRYOVER, load_quiz_results

    started = time.perf_counter()
    directory = Path(QUIZ_DATA_DIR if quiz_dir is None else quiz_dir)
    found = _quiz_results_files(directory)
    key = (str(directory), _quiz_fingerprint([path for _, path in found]))
    if not force and key in _PACING_CACHE:
        return _PACING_CACHE[key]

    def report(done: int, total: int, message: str) -> None:
        if progress is not None:
            progress(done, total, message)

    # 1. every trial of every participant
    by_participant: Dict[str, List[float]] = {}
    total_steps = len(found) + 2
    for index, (participant, results_path) in enumerate(found, start=1):
        report(index, total_steps, f"reading {participant} ({results_path.parent.name})")
        try:
            results = load_quiz_results(results_path)
        except (OSError, ValueError, TypeError) as exc:
            # A half-written or hand-edited quiz is not a reason to
            # abandon the fit; the rest of the corpus is still good.
            log.warning("skipping %s while fitting human pacing: %s", results_path.parent.name, exc)
            continue
        for result in results:
            if result.timed_out or result.validity == VALIDITY_INVALID_CARRYOVER:
                continue
            if result.timing_error_s is None or result.timing_error_s <= 0:
                continue
            by_participant.setdefault(participant, []).append(result.timing_error_s)

    pooled = [rt for values in by_participant.values() for rt in values]
    if len(pooled) < MIN_HUMAN_TRIALS:
        report(total_steps, total_steps, "not enough trials - using the fallback fit")
        fit = HumanPacing(FALLBACK_HUMAN_MEDIAN_S, FALLBACK_HUMAN_LOG_SIGMA,
                          analysis_s=time.perf_counter() - started)
        _PACING_CACHE[key] = fit
        return fit

    # 2. per participant, then pooled
    report(len(found) + 1, total_steps, f"fitting {len(by_participant)} participants")
    people = []
    for participant, values in sorted(by_participant.items()):
        logs = [math.log(rt) for rt in values]
        people.append(
            ParticipantPacing(
                participant=participant,
                median_s=math.exp(statistics.mean(logs)),
                log_sigma=statistics.stdev(logs) if len(logs) > 1 else 0.0,
                trials=len(values),
            )
        )
    pooled_logs = [math.log(rt) for rt in pooled]

    # 3. which family, and how certain (scipy only)
    report(len(found) + 2, total_steps, "comparing distribution families")
    families, median_ci, sigma_ci = _compare_families(by_participant, people)

    fit = HumanPacing(
        # exp(mean of logs) is the lognormal's median, not its mean.
        median_s=math.exp(statistics.mean(pooled_logs)),
        log_sigma=statistics.stdev(pooled_logs),
        participants=people,
        trials=len(pooled),
        source=f"measured from {len(people)} participants, {len(pooled):,} trials",
        families=families,
        median_ci=median_ci,
        log_sigma_ci=sigma_ci,
        analysis_s=time.perf_counter() - started,
    )
    _PACING_CACHE[key] = fit
    return fit


def _compare_families(
    by_participant: Dict[str, List[float]], people: List[ParticipantPacing]
) -> Tuple[Dict[str, Dict[str, float]], Optional[Tuple[float, float]], Optional[Tuple[float, float]]]:
    """Candidate fits, their quality per participant, and a bootstrap
    interval on the pooled parameters.

    Split out because it is the only part needing scipy: a machine
    without it should still get a working pacing model rather than an
    ImportError. Fills in each ParticipantPacing.ks in passing."""
    try:
        import numpy as np
        from scipy import stats
    except ImportError:
        log.info("scipy/numpy not available - skipping the distribution comparison")
        return {}, None, None

    pooled = np.asarray([rt for values in by_participant.values() for rt in values])
    families: Dict[str, Dict[str, float]] = {}
    for name, dist, params in (
        ("lognormal", stats.lognorm, stats.lognorm.fit(pooled, floc=0)),
        ("ex_gaussian", stats.exponnorm, stats.exponnorm.fit(pooled)),
        ("shifted_lognormal", stats.lognorm, stats.lognorm.fit(pooled)),
    ):
        log_likelihood = float(np.sum(dist.logpdf(pooled, *params)))
        families[name] = {
            "ks": float(stats.kstest(pooled, dist.cdf, args=params).statistic),
            "aic": 2 * len(params) - 2 * log_likelihood,
            "parameters": float(len(params)),
        }

    # Each participant against their *own* fitted lognormal, which is
    # what the hierarchical sampler will actually draw from.
    for person in people:
        values = np.asarray(by_participant[person.participant])
        person.ks = float(
            stats.kstest(values, stats.lognorm.cdf, args=(person.log_sigma, 0.0, person.median_s)).statistic
        )

    logs = np.log(pooled)
    # A fixed seed: this interval is a property of the data, and must not
    # move between two reads of the same corpus.
    rng = np.random.default_rng(0)
    medians = np.empty(BOOTSTRAP_RESAMPLES)
    sigmas = np.empty(BOOTSTRAP_RESAMPLES)
    for index in range(BOOTSTRAP_RESAMPLES):
        sample = rng.choice(logs, size=logs.size, replace=True)
        medians[index] = math.exp(float(sample.mean()))
        sigmas[index] = float(sample.std(ddof=1))
    return (
        families,
        (float(np.percentile(medians, 2.5)), float(np.percentile(medians, 97.5))),
        (float(np.percentile(sigmas, 2.5)), float(np.percentile(sigmas, 97.5))),
    )


def human_pacing_for(config: BenchmarkConfig) -> HumanPacing:
    """What `human` mode will use for this config: the operator's own
    sigma if they gave one, otherwise the current fit."""
    if config.human_log_sigma is None:
        return measure_human_pacing()
    return HumanPacing(
        median_s=config.interval_s,
        log_sigma=config.human_log_sigma,
        source="sigma given on the command line",
    )


def expected_interval_s(config: BenchmarkConfig) -> float:
    """The mean wait a run will actually average.

    Only `human` differs from the interval it was given: a lognormal's
    mean sits above its median, so quoting the median as the run length
    would under-estimate a 1000-probe run by minutes."""
    if config.interval_mode == "human":
        return config.interval_s * math.exp(human_pacing_for(config).log_sigma ** 2 / 2.0)
    return config.interval_s


def describe_pacing(config: BenchmarkConfig) -> str:
    """One phrase for how the probes are spaced, with the actual range.

    Shared by the CLI's own output and the benchmark window's Pacing row
    so the range the operator is shown can never disagree with the range
    the run uses."""
    mean_ms = config.interval_s * 1000.0
    seed = f", seed {config.interval_seed}" if config.interval_seed is not None else ""
    if config.interval_mode == "uniform":
        spread = mean_ms * config.interval_jitter
        return (
            f"{mean_ms - spread:.0f}-{mean_ms + spread:.0f} ms intervals "
            f"(uniform +/-{config.interval_jitter * 100:.0f}%, mean {mean_ms:.0f} ms{seed})"
        )
    if config.interval_mode == "poisson":
        return (
            f"exponential intervals (poisson, mean {mean_ms:.0f} ms, "
            f"capped at {mean_ms * POISSON_MAX_INTERVAL_FACTOR:.0f} ms{seed})"
        )
    if config.interval_mode == "human":
        # The central 90%, which is the honest way to state the spread of
        # a skewed distribution - a +/- figure would imply a symmetry it
        # does not have.
        fit = human_pacing_for(config)
        low = mean_ms * math.exp(-1.645 * fit.log_sigma)
        high = mean_ms * math.exp(1.645 * fit.log_sigma)
        return (
            f"{low:.0f}-{high:.0f} ms intervals 90% of the time (human reaction time, lognormal "
            f"median {mean_ms:.0f} ms, sigma {fit.log_sigma:.3f} {fit.source}{seed})"
        )
    return f"{mean_ms:.0f} ms intervals (fixed)"


class SimulatedStudentResponder:
    """Internal student endpoint for a self-contained network run.

    It sends the same immediate transport acknowledgement as the real
    Student Client using the same WebSocket implementation, but owns no
    Qt UI or hardware. That boundary prevents simulated network timing
    from being mistaken for physical cue latency.
    """

    def __init__(self, client: RemoteWebSocketClient):
        self.client = client

    def on_message(self, envelope: Dict[str, Any]) -> None:
        if envelope.get("type") != TYPE_LATENCY_PROBE:
            return
        payload = envelope.get("payload") or {}
        probe_id = payload.get("probe_id")
        if not probe_id:
            return
        receive_mono, receive_wall = mono_ns(), wall_ns()
        self.client.send(
            TYPE_LATENCY_RECEIVED,
            {
                "probe_id": probe_id,
                "probe_message_id": envelope.get("message_id"),
                "student_receive_wall_ns": receive_wall,
                "student_receive_monotonic_ns": receive_mono,
                "responder": "built_in_simulated_student",
            },
        )


class StudentPresenceWatcher:
    """Notices when a student is actually in the room.

    The relay broadcasts room membership as `presence`, so an
    external-student run waits on the same signal the Teacher Client
    waits on instead of polling REST. Probing before the student is there
    would count every early probe as transport loss.
    """

    def __init__(self, ignore_username: str = ""):
        # Set when the built-in responder holds the student seat, so its
        # own arrival cannot satisfy a wait for a *real* Student Client.
        self.ignore_username = ignore_username
        self.student_username: Optional[str] = None
        self._joined = threading.Event()

    def on_message(self, envelope: Dict[str, Any]) -> None:
        if envelope.get("type") != TYPE_PRESENCE:
            return
        for member in (envelope.get("payload") or {}).get("members") or []:
            if member.get("role") != "student":
                continue
            username = str(member.get("username") or "")
            if self.ignore_username and username == self.ignore_username:
                continue
            self.student_username = username
            self._joined.set()
            return

    @property
    def joined(self) -> bool:
        return self._joined.is_set()

    def wait(self, timeout: float) -> bool:
        return self._joined.wait(timeout)


def create_benchmark_room(teacher_api: RemoteApiClient) -> Dict[str, str]:
    """Make the temporary room this run measures in.

    The teacher account owns it, which is what puts that account in the
    room - only the owner's response carries the join code the student
    side needs."""
    room = teacher_api.create_room(f"Network latency benchmark {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return {"room_id": str(room["room_id"]), "join_code": str(room.get("join_code") or "")}


def format_room_members(members: List[Dict[str, Any]]) -> str:
    """The membership the relay confirms, not the one we assume.

    Printed after both joins so a run shows on its face that the teacher
    and the student really are in the same room; a benchmark whose two
    endpoints were in different rooms would simply lose every probe."""
    if not members:
        return "none"
    return ", ".join(
        f"{member.get('role', '?')} {member.get('username', '?')}"
        + ("" if member.get("online", True) else " (offline)")
        for member in members
    )


def report_room_membership(teacher_api: RemoteApiClient, room_id: str) -> None:
    """Best-effort: a membership read failing is not a reason to abandon
    a run whose two joins already succeeded."""
    try:
        members = teacher_api.room_members(room_id)
    except RemoteApiError as exc:
        print(f"{ROOM_LINE} could not read the membership back: {exc}")
        return
    print(f"{ROOM_LINE} members confirmed: {format_room_members(members)}")


class LatencyBenchmark:
    """Drives the probes over an already-connected WebSocket client.

    Takes a client rather than making one, so a test can hand it a stub
    and so a real run reuses the *same* Teacher connection a session
    would rather than measuring repeated connection setup."""

    def __init__(self, client: RemoteWebSocketClient, config: Optional[BenchmarkConfig] = None):
        self.client = client
        self.config = config or BenchmarkConfig()
        if self.config.interval_mode not in INTERVAL_MODES:
            raise ValueError(
                f"unknown interval mode {self.config.interval_mode!r}; expected one of {INTERVAL_MODES}"
            )
        if not 0.0 <= self.config.interval_jitter < 1.0:
            raise ValueError("interval jitter must be a fraction in [0, 1)")
        if self.config.interval_seed is None:
            self.config.interval_seed = random.randrange(2**32)
        # Its own generator, not the global one: a run's pacing must not
        # depend on whatever else in the process drew a random number.
        self._pacing = random.Random(self.config.interval_seed)
        # Fitted once here rather than per probe. Deliberately not
        # written back into the config: config records what was asked for
        # (None = "fit it"), pacing.human records what was used.
        self.human_pacing = human_pacing_for(self.config)
        self.samples: Dict[str, ProbeSample] = {}
        self.order: List[str] = []
        self.offsets = ClockOffsetEstimator()
        self._lock = threading.Lock()
        self._done = threading.Event()

    # -- receiving -----------------------------------------------------

    def on_message(self, envelope: Dict[str, Any]) -> None:
        """Called on the client's receive thread. Only ever stamps
        arrival with *this* machine's monotonic clock."""
        arrived = mono_ns()
        msg_type = envelope.get("type")
        payload = envelope.get("payload") or {}
        probe_id = payload.get("probe_id")
        if not probe_id:
            return

        with self._lock:
            sample = self.samples.get(probe_id)
            if sample is None:
                return

            if msg_type == TYPE_LATENCY_ACK and sample.server_ack_rtt_ns is None:
                sample.server_ack_rtt_ns = arrived - sample.sent_mono_ns
            elif msg_type == TYPE_LATENCY_RECEIVED and sample.rtt_ns is None:
                sample.rtt_ns = arrived - sample.sent_mono_ns
                sample.student_receive_wall_ns = payload.get("student_receive_wall_ns")
                sample.server_receive_wall_ns = (envelope.get("server") or {}).get("receive_wall_ns")
                self._add_offset_exchange(sample, envelope, arrived)
            elif msg_type == TYPE_LATENCY_PRESENTED and sample.presented_rtt_ns is None:
                sample.presented_rtt_ns = arrived - sample.sent_mono_ns
                sample.student_dispatch_ns = payload.get("dispatch_ns")

    def _add_offset_exchange(self, sample: ProbeSample, envelope: Dict[str, Any], arrived_mono: int) -> None:
        """The four-timestamp exchange NTP's geometry uses, built from the
        probe and its ack: our send wall time, the student's receive wall
        time, the student's reply wall time, our receive wall time. Used
        only to *quantify* the clock offset, never as a latency."""
        t2 = sample.student_receive_wall_ns
        t3 = envelope.get("sent_at_unix_ns")
        if not t2 or not t3:
            return
        # Our receive wall time, derived from the monotonic arrival stamp
        # via this machine's own clock pair.
        t4 = wall_ns() - (mono_ns() - arrived_mono)
        self.offsets.add_exchange(sample.sent_wall_ns, int(t2), int(t3), int(t4))

    # -- running -------------------------------------------------------

    def next_interval(self) -> float:
        """How long to wait before the next probe.

        Every mode has mean `interval_s`, so switching pacing changes the
        spacing pattern and nothing else - not the probe count, not the
        rate, not the expected run length."""
        mean = self.config.interval_s
        if self.config.interval_mode == "uniform":
            spread = mean * self.config.interval_jitter
            return self._pacing.uniform(mean - spread, mean + spread)
        if self.config.interval_mode == "poisson":
            return min(self._pacing.expovariate(1.0 / mean), mean * POISSON_MAX_INTERVAL_FACTOR)
        if self.config.interval_mode == "human":
            # `mean` is the *median* here: exp(mu) is a lognormal's
            # median, and the median is what was measured from the
            # participants' reaction times.
            #
            # Two levels, not one: pick a participant, then draw from
            # that participant's own distribution, scaled so the group
            # median lands on the requested interval. Pooling every
            # trial into a single lognormal would blur within-person
            # spread together with between-person differences and
            # overstate the first - see measure_human_pacing.
            people = self.human_pacing.participants
            if people:
                person = self._pacing.choice(people)
                median = mean * (person.median_s / self.human_pacing.median_s)
                draw = self._pacing.lognormvariate(math.log(median), person.log_sigma)
            else:
                draw = self._pacing.lognormvariate(math.log(mean), self.human_pacing.log_sigma)
            return min(draw, mean * HUMAN_MAX_INTERVAL_FACTOR)
        return mean

    def run(self, progress: Optional[Any] = None) -> Dict[str, Any]:
        total = self.config.warmup + self.config.count
        for i in range(total):
            warmup = i < self.config.warmup
            self._send_probe(seq=i, warmup=warmup)
            if progress is not None:
                progress(i + 1, total)
            time.sleep(self.next_interval())

        # Give the last probes their full timeout before declaring loss.
        time.sleep(self.config.ack_timeout_s)
        return self.summarize()

    def _send_probe(self, seq: int, warmup: bool) -> None:
        probe_id = str(uuid.uuid4())
        sample = ProbeSample(seq=seq, probe_id=probe_id, warmup=warmup)
        with self._lock:
            self.samples[probe_id] = sample
            self.order.append(probe_id)

        payload = {
            "probe_id": probe_id,
            "seq": seq,
            "warmup": warmup,
            "note": self.config.note,
            "finger": self.config.finger,
            "trigger_cue": self.config.trigger_cue,
        }
        # Stamped as late as possible before the send, so serialisation
        # cost lands inside the measured interval rather than before it.
        sample.sent_wall_ns = wall_ns()
        sample.sent_mono_ns = mono_ns()
        self.client.send(TYPE_LATENCY_PROBE, payload, message_id=probe_id)

    # -- results -------------------------------------------------------

    def measured_samples(self) -> List[ProbeSample]:
        """Warm-up probes excluded - they are kept in the CSV, flagged, so
        the exclusion is visible rather than assumed."""
        with self._lock:
            return [self.samples[pid] for pid in self.order if not self.samples[pid].warmup]

    def all_samples(self) -> List[ProbeSample]:
        with self._lock:
            return [self.samples[pid] for pid in self.order]

    def pacing_summary(self) -> Dict[str, Any]:
        """What the probes were *actually* spaced by, next to what was
        asked for.

        Worth recording twice over: the loop sleeps after each send, so
        every gap also carries that probe's send cost, and a randomised
        run should be able to show its spread rather than assert it."""
        stamps = [s.sent_wall_ns for s in self.all_samples() if s.sent_wall_ns]
        gaps_ms = [(later - earlier) / 1e6 for earlier, later in zip(stamps, stamps[1:])]
        pacing: Dict[str, Any] = {
            "mode": self.config.interval_mode,
            "nominal_mean_ms": self.config.interval_s * 1000.0,
            "jitter": self.config.interval_jitter if self.config.interval_mode == "uniform" else None,
            "seed": self.config.interval_seed,
            "gaps": len(gaps_ms),
        }
        if self.config.interval_mode == "human":
            # Which population this run was paced like, recorded with it:
            # the fit moves as participants are added.
            pacing["human"] = self.human_pacing.as_dict()
        if gaps_ms:
            pacing.update(
                realised_mean_ms=sum(gaps_ms) / len(gaps_ms),
                realised_min_ms=min(gaps_ms),
                realised_max_ms=max(gaps_ms),
            )
        return pacing

    def summarize(self) -> Dict[str, Any]:
        measured = self.measured_samples()
        lost = sum(1 for s in measured if s.lost)
        rtts = [s.rtt_ns for s in measured if s.rtt_ns is not None]

        offset = self.offsets.estimate(synchronised=self.config.clocks_synced)
        receive_deltas = [s.wall_delta_ns for s in measured if s.wall_delta_ns is not None]
        if self.config.built_in_student:
            one_way = summarize_latency(
                receive_deltas,
                label="one_way_shared_host_clock",
                caveat=(
                    "Built-in teacher and student endpoints share one host clock, so this is the measured "
                    "teacher-send -> simulated-student-receive duration through the relay. It does not "
                    "include a real Student UI or hardware."
                ),
            )
        else:
            one_way = one_way_estimate(
                rtts,
                clock_offset=offset,
                receive_deltas=receive_deltas,
            )

        presented = [s.presented_rtt_ns for s in measured if s.presented_rtt_ns is not None]
        metrics: Dict[str, LatencyStats] = {
            "transport_rtt": summarize_latency(
                rtts,
                lost=lost,
                label="transport_rtt",
                caveat="PRIMARY transport metric: both stamps from the teacher's own monotonic clock.",
            ),
            "teacher_to_server_ack": summarize_latency(
                [s.server_ack_rtt_ns for s in measured if s.server_ack_rtt_ns is not None],
                label="teacher_to_server_ack",
                caveat="Round trip to the relay only - isolates the teacher->server hop.",
            ),
        }
        if not self.config.built_in_student:
            metrics["cue_presented_rtt"] = summarize_latency(
                presented,
                lost=len(measured) - len(presented) if self.config.trigger_cue else 0,
                label="cue_presented_rtt",
                caveat=(
                    "Includes the student's local cue dispatch. Software dispatch/render timing - "
                    "NOT physical LED or actuator onset."
                ),
            )
            metrics["student_local_dispatch"] = summarize_latency(
                [s.student_dispatch_ns for s in measured if s.student_dispatch_ns is not None],
                label="student_local_dispatch",
                caveat=(
                    "Measured on the student's own monotonic clock and reported back as a duration. "
                    "Software dispatch/render timing, not physical cue onset."
                ),
            )
        metrics["one_way"] = one_way

        return {
            "config": self.config.as_dict(),
            "counts": {
                "attempted": len(measured),
                "warmup": self.config.warmup,
                "acknowledged": len(rtts),
                "lost": lost,
                "loss_rate": (lost / len(measured)) if measured else None,
            },
            "clock_offset": offset.as_dict() if offset else None,
            "pacing": self.pacing_summary(),
            "metrics": {name: stats.as_dict() for name, stats in metrics.items()},
            "notes": [
                "Transport RTT is the primary metric; it needs no clock synchronisation.",
                (
                    f"Probe pacing: {self.config.interval_mode}. Randomised pacing (RFC 2330 style) stops a "
                    "fixed period from sampling the same phase of anything periodic in the path; the mean "
                    "interval is unchanged. Repeat this pacing with --interval-seed "
                    f"{self.config.interval_seed}."
                    if self.config.interval_mode != "fixed"
                    else "Probe pacing: fixed period."
                ),
                one_way.caveat or "",
                "All cue timings are software dispatch/render moments, not physical LED/actuator onset.",
            ],
        }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def save_run(samples: List[ProbeSample], summary: Dict[str, Any], run_id: Optional[str] = None,
             base_dir: Path = RESULTS_DIR) -> Path:
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
    directory = Path(base_dir) / run_id
    directory.mkdir(parents=True, exist_ok=True)

    with open(directory / "samples.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for sample in samples:
            writer.writerow(sample.as_row())

    summary = dict(summary)
    summary["run_id"] = run_id
    summary["written_at"] = time.time()
    with open(directory / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    _write_figures(samples, summary, directory)
    return directory


def _write_figures(samples: List[ProbeSample], summary: Dict[str, Any], directory: Path) -> List[Path]:
    """The run's report figures. See latency_plots for what each one is
    for and why it is drawn the way it is.

    The human-scale figure needs the participants' reaction-time fit. A
    human-paced run already carries the population it used, and that is
    the right reference for it; any other run borrows the current fit,
    which is safe to compute here because measurement has finished and
    nothing this does can affect a timing figure."""
    from .latency_plots import write_figures

    context = dict(summary)
    if not ((summary.get("pacing") or {}).get("human")):
        try:
            fit = measure_human_pacing()
        except (ImportError, OSError, ValueError) as exc:
            log.info("no reaction-time reference for the scale figure: %s", exc)
        else:
            context["pacing"] = {**(summary.get("pacing") or {}), "human": fit.as_dict()}
    return write_figures(samples, context, directory)


def analyse_summary(summary: Dict[str, Any]) -> List[str]:
    """A few plain readings of one run, for the results page.

    Descriptions, not verdicts: this says what the distribution's shape
    is and what scale the numbers sit at, and leaves "is that acceptable"
    to whoever set the requirement. The one external number it brings in
    is this study's own measured reaction time, because a transport delay
    only means something next to the human delay it is added to."""
    lines: List[str] = []
    counts = summary.get("counts") or {}
    rtt = (summary.get("metrics") or {}).get("transport_rtt") or {}
    attempted, lost = counts.get("attempted") or 0, counts.get("lost") or 0
    if attempted:
        lines.append(
            f"{attempted} measured probes, {lost} lost ({(counts.get('loss_rate') or 0) * 100:.2f}%)"
            + (f", {counts['warmup']} warm-up excluded" if counts.get("warmup") else "")
        )

    median, p95, p99 = rtt.get("median_ms"), rtt.get("p95_ms"), rtt.get("p99_ms")
    if median:
        lines.append(
            f"round trip: median {median:.1f} ms, p95 {p95:.1f} ms, p99 {p99:.1f} ms, "
            f"worst {rtt.get('max_ms', float('nan')):.1f} ms"
        )
        if p95:
            lines.append(
                f"p95 is {p95 / median:.1f}x the median"
                + (
                    " - the spread comes from a tail, not from the typical probe"
                    if p95 / median >= 2.0
                    else " - a tight distribution, the typical probe is representative"
                )
            )
        # Never fits anything: this is a page of past results, and it
        # must not stall on a corpus scan to draw a line of text. The
        # run's own recorded population comes first - that is the one it
        # was paced against - then a fit already in hand, else nothing.
        recorded = ((summary.get("pacing") or {}).get("human")) or {}
        reference_s, reference_source = recorded.get("median_s"), recorded.get("source")
        if reference_s is None:
            cached = cached_human_pacing()
            if cached is not None:
                reference_s, reference_source = cached.median_s, cached.source
        if reference_s:
            lines.append(
                f"for scale, the median is {median / (reference_s * 1000) * 100:.1f}% of the "
                f"{reference_s * 1000:.0f} ms median reaction time ({reference_source})"
            )

    pacing = summary.get("pacing") or {}
    if pacing.get("mode"):
        line = f"paced {pacing['mode']}, nominal {pacing.get('nominal_mean_ms', 0):.0f} ms"
        if pacing.get("realised_mean_ms") is not None:
            line += f", realised mean {pacing['realised_mean_ms']:.0f} ms"
        if pacing["mode"] != "fixed" and pacing.get("seed") is not None:
            line += f", repeat with --interval-seed {pacing['seed']}"
        lines.append(line)

    config = summary.get("config") or {}
    if config.get("built_in_student"):
        lines.append("built-in student: this is the network/relay path only, not Student UI or hardware")
    return lines


def format_summary(summary: Dict[str, Any]) -> str:
    lines = ["", "=" * 72, "Remote guidance latency benchmark", "=" * 72]
    counts = summary.get("counts", {})
    lines.append(
        f"probes: {counts.get('attempted', 0)} measured "
        f"(+{counts.get('warmup', 0)} warm-up, excluded), "
        f"lost {counts.get('lost', 0)} "
        f"({(counts.get('loss_rate') or 0) * 100:.2f}%)"
    )
    offset = summary.get("clock_offset")
    if offset:
        if (summary.get("config") or {}).get("built_in_student"):
            clock_label = "shared-host client clock"
        elif offset["synchronised"]:
            clock_label = "NTP-synced asserted"
        else:
            clock_label = "synchronisation NOT confirmed"
        lines.append(
            f"clock offset estimate: {offset['offset_ms']:+.3f} ms "
            f"+/-{offset['uncertainty_ms']:.3f} ms over {offset['samples']} exchanges "
            f"({clock_label})"
        )
    pacing = summary.get("pacing")
    if pacing:
        line = f"pacing: {pacing['mode']}, nominal mean {pacing['nominal_mean_ms']:.0f} ms"
        if pacing.get("realised_mean_ms") is not None:
            line += (
                f"; realised {pacing['realised_min_ms']:.0f}-{pacing['realised_max_ms']:.0f} ms, "
                f"mean {pacing['realised_mean_ms']:.0f} ms over {pacing['gaps']} gaps"
            )
        if pacing["mode"] != "fixed":
            line += f" (seed {pacing['seed']})"
        lines.append(line)
    lines.append("")
    header = f"{'metric':<24}{'n':>6}{'mean':>10}{'sd':>10}{'median':>10}{'p95':>10}{'p99':>10}{'max':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, stats in summary.get("metrics", {}).items():
        lines.append(
            f"{name:<24}{stats.get('n', 0):>6}"
            + "".join(
                f"{(stats.get(key) if stats.get(key) is not None else float('nan')):>10.2f}"
                for key in ("mean_ms", "sd_ms", "median_ms", "p95_ms", "p99_ms", "max_ms")
            )
        )
    lines.append("")
    lines.append("(all times in milliseconds)")
    for name, stats in summary.get("metrics", {}).items():
        if stats.get("caveat"):
            lines.append(f"  * {name}: {stats['caveat']}")
    lines.append("=" * 72)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    remote = RemoteGuidanceConfig.load()
    p = argparse.ArgumentParser(
        prog="remote_latency_benchmark.py",
        description="Measure relayed teacher -> student guidance latency",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--server", default=remote.network.server_url, help="relay base URL")
    p.add_argument(
        "--username", "--teacher-username", dest="teacher_username",
        # network.username is shared and may contain the Student Client's
        # most recent login, so it is not a safe role default here.
        default="demoteacher", help="teacher account",
    )
    p.add_argument(
        "--password", "--teacher-password", dest="teacher_password",
        default=None, help="teacher password; prompted for if omitted (never stored)",
    )
    p.add_argument("--student-username", default="demostudent", help="built-in student account")
    p.add_argument(
        "--student-password", default=None,
        help="built-in student password; prompted for if omitted (never stored)",
    )
    p.add_argument(
        "--external-student", action="store_true",
        help="use a real Student Client instead of the built-in simulated student",
    )
    p.add_argument(
        "--room-id", default="",
        help=(
            "join this existing room instead of creating one; allowed only with --external-student, "
            "for a Student Client that is already in a room of its own"
        ),
    )
    p.add_argument(
        "--wait-for-student", type=float, default=DEFAULT_STUDENT_WAIT_S,
        help=(
            "seconds to wait for a real Student Client to join the room this tool created "
            "(--external-student without --room-id)"
        ),
    )
    p.add_argument("--count", type=int, default=DEFAULT_COUNT, help="measured probes")
    p.add_argument(
        "--interval", type=float, default=None,
        help=(
            "seconds between probes - the mean wait once the pacing is randomised, or its median under "
            f"--interval-mode human. Default {DEFAULT_INTERVAL_S} s, or in human mode the participant "
            "median fitted from data/quiz at run time"
        ),
    )
    p.add_argument(
        "--interval-mode", choices=INTERVAL_MODES, default=DEFAULT_INTERVAL_MODE,
        help=(
            "probe pacing: fixed period, uniform jitter around --interval, or poisson "
            "(exponential waits, RFC 2330's unbiased choice). All three have the same mean"
        ),
    )
    p.add_argument(
        "--interval-jitter", type=float, default=DEFAULT_INTERVAL_JITTER,
        help="uniform mode only: each wait is --interval plus or minus this fraction of it",
    )
    p.add_argument(
        "--interval-seed", type=int, default=None,
        help="pass a previous run's recorded seed to repeat its pacing exactly; otherwise one is drawn and recorded",
    )
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP, help="probes recorded but excluded from stats")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="ack timeout in seconds")
    p.add_argument("--trigger-cue", action="store_true", help="ask the student to fire its real LED/haptic cue")
    p.add_argument(
        "--clocks-synced",
        action="store_true",
        help=(
            "assert both hosts are NTP-synchronised, so a clock-corrected one-way latency may be reported "
            "alongside its offset uncertainty. Without this, one-way is shown as RTT/2 and labelled an estimate."
        ),
    )
    p.add_argument("--no-verify-tls", action="store_true", help="accept a self-signed development certificate")
    p.add_argument("--run-id", default=None, help="output folder name under data/remote_guidance/latency/")
    p.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    args = build_parser().parse_args(argv)

    if args.interval_mode == "human":
        # Fitted before anything is dialled, so the log says what the run
        # is about to imitate - and so a slow corpus is visibly the fit,
        # not a stalled connection.
        def pacing_progress(done: int, total: int, message: str) -> None:
            if total and (done % 100 == 0 or done >= total):
                print(f"  fitting human pacing {done}/{total}: {message}", flush=True)

        print("fitting human pacing from the study's quiz data...")
        fit = measure_human_pacing(progress=pacing_progress)
        print(
            f"human pacing: {fit.source}; median {fit.median_s * 1000:.0f} ms, sigma {fit.log_sigma:.3f} "
            f"({fit.analysis_s:.1f}s)"
        )
        if fit.families:
            print(
                "  pooled family check: "
                + ", ".join(
                    f"{name} KS {stats['ks']:.3f} AIC {stats['aic']:.0f}"
                    for name, stats in sorted(fit.families.items(), key=lambda item: item[1]["aic"])
                )
            )
        # Said plainly, because the pooled comparison above is not what
        # the probes are drawn from.
        print(
            f"  sampling: per-participant lognormal - one of the {fit.participant_count} participants is drawn "
            "per probe, then a wait from that participant's own distribution"
            if fit.participants
            else "  sampling: pooled lognormal"
        )
    if args.interval is None:
        # Each mode's own default, so `--interval-mode human` alone
        # reproduces the participants' timing rather than pacing their
        # distribution around an unrelated 500 ms.
        args.interval = (
            measure_human_pacing().median_s if args.interval_mode == "human" else DEFAULT_INTERVAL_S
        )
    if not 0.0 <= args.interval_jitter < 1.0:
        print("error: --interval-jitter must be a fraction in [0, 1)", file=sys.stderr)
        return 2
    if not args.external_student and args.room_id:
        print("error: --room-id is only used with --external-student", file=sys.stderr)
        return 2
    if not args.external_student and args.trigger_cue:
        print(
            "error: --trigger-cue requires --external-student because the built-in student owns no UI or hardware",
            file=sys.stderr,
        )
        return 2

    verify_tls = not args.no_verify_tls
    teacher_api = RemoteApiClient(args.server, verify_tls=verify_tls)
    student_api: Optional[RemoteApiClient] = None
    teacher_client: Optional[RemoteWebSocketClient] = None
    student_client: Optional[RemoteWebSocketClient] = None
    created_room_id: Optional[str] = None
    benchmark: Optional[LatencyBenchmark] = None
    summary: Optional[Dict[str, Any]] = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt_run(_signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_run)

    try:
        teacher_password = args.teacher_password or getpass.getpass(
            f"password for {args.teacher_username}: "
        )
        try:
            teacher_session = teacher_api.login(args.teacher_username, teacher_password)
        finally:
            teacher_password = ""
        if teacher_session.role != "teacher":
            raise RemoteApiError(
                f"{args.teacher_username!r} is a {teacher_session.role!r} account, not a teacher"
            )

        room_id = args.room_id
        if args.external_student:
            print("mode: external Student Client (real Student software/hardware path)")
            if room_id:
                print(f"{ROOM_LINE} using the existing room {room_id} that Student Client joined")
            else:
                room = create_benchmark_room(teacher_api)
                room_id = room["room_id"]
                created_room_id = room_id
                print(f"{ROOM_LINE} created {room_id}")
                print(f"{ROOM_LINE} join code {room['join_code']}")
                print(f"{ROOM_LINE} teacher {args.teacher_username} is its owner")
                print(
                    f"{ROOM_LINE} enter join code {room['join_code']} in Student Client - "
                    f"probing starts once the relay reports it in the room"
                )
        else:
            student_api = RemoteApiClient(args.server, verify_tls=verify_tls)
            student_password = args.student_password or getpass.getpass(
                f"password for {args.student_username}: "
            )
            try:
                student_session = student_api.login(args.student_username, student_password)
            finally:
                student_password = ""
            if student_session.role != "student":
                raise RemoteApiError(
                    f"{args.student_username!r} is a {student_session.role!r} account, not a student"
                )

            room = create_benchmark_room(teacher_api)
            room_id = room["room_id"]
            created_room_id = room_id
            print("mode: built-in simulated student (network/relay path only)")
            print(f"{ROOM_LINE} created {room_id}")
            print(f"{ROOM_LINE} join code {room['join_code']}")
            print(f"{ROOM_LINE} teacher {args.teacher_username} is its owner")
            student_api.join_room(room["join_code"])
            print(f"{ROOM_LINE} student {args.student_username} joined with that code")

            student_client = RemoteWebSocketClient(
                ws_url=_ws_url(args.server, room_id),
                access_token=student_session.access_token,
                room_id=room_id,
                verify_tls=verify_tls,
                auto_reconnect=False,
            )
            responder = SimulatedStudentResponder(student_client)
            student_client.callbacks = ClientCallbacks(on_message=responder.on_message)
            student_client.start()
            if not student_client.wait_until_connected(timeout=20.0):
                print("error: built-in student could not connect to the room's WebSocket", file=sys.stderr)
                return 1

        config = BenchmarkConfig(
            count=args.count,
            interval_s=args.interval,
            interval_mode=args.interval_mode,
            interval_jitter=args.interval_jitter,
            interval_seed=args.interval_seed,
            warmup=args.warmup,
            ack_timeout_s=args.timeout,
            trigger_cue=args.trigger_cue,
            # Built-in teacher and student endpoints run in this process,
            # so they share one host clock rather than merely being NTP-close.
            clocks_synced=args.clocks_synced or not args.external_student,
            built_in_student=not args.external_student,
        )
        teacher_client = RemoteWebSocketClient(
            ws_url=_ws_url(args.server, room_id),
            access_token=teacher_session.access_token,
            room_id=room_id,
            verify_tls=verify_tls,
            auto_reconnect=False,  # a reconnect mid-run would invalidate the sample
        )
        benchmark = LatencyBenchmark(teacher_client, config)
        presence = StudentPresenceWatcher(
            ignore_username="" if args.external_student else args.student_username
        )

        def on_teacher_message(envelope: Dict[str, Any]) -> None:
            # The benchmark first, always: it stamps arrival from the
            # monotonic clock on its very first line, and anything run
            # ahead of it would be added to every measured RTT.
            benchmark.on_message(envelope)
            presence.on_message(envelope)

        teacher_client.callbacks = ClientCallbacks(on_message=on_teacher_message)
        teacher_client.start()

        if not teacher_client.wait_until_connected(timeout=20.0):
            print("error: benchmark teacher could not connect to the room's WebSocket", file=sys.stderr)
            return 1

        if args.external_student and created_room_id is not None:
            try:
                joined = presence.wait(args.wait_for_student)
            except KeyboardInterrupt:
                # Stop during a wait that can last minutes is an ordinary
                # thing to press, not a crash.
                print("\ninterrupted while waiting for a Student Client to join")
                return 1
            if not joined:
                print(
                    f"error: no Student Client joined the benchmark room within {args.wait_for_student:.0f}s",
                    file=sys.stderr,
                )
                return 1
        if presence.student_username:
            print(f"{ROOM_LINE} student {presence.student_username} is connected")
        if created_room_id is not None:
            # Read back only now that both sockets are up, so the online
            # flags mean "on the WebSocket", not merely "a member".
            report_room_membership(teacher_api, created_room_id)

        total = config.warmup + config.count
        print(
            f"probing: {config.count} measured + {config.warmup} warm-up at {describe_pacing(config)} "
            f"over persistent WebSockets (~{total * expected_interval_s(config) / 60:.1f} min)"
        )
        if not args.external_student:
            print(
                "note: teacher and simulated student share this process's host clock; this run measures the "
                "network/relay path, not Student UI, LED, haptic or physical onset."
            )
        elif not args.clocks_synced:
            print(
                "note: clock synchronisation not asserted - one-way latency will be shown as RTT/2, "
                "a symmetry-based estimate. Check both hosts (e.g. time.is or `chronyc tracking`) and pass "
                "--clocks-synced to get a clock-corrected figure instead."
            )

        def progress(done: int, total_probes: int) -> None:
            if done % 50 == 0 or done == total_probes:
                print(f"  {done}/{total_probes}", flush=True)

        try:
            summary = benchmark.run(progress=progress)
        except KeyboardInterrupt:
            print("\ninterrupted - summarising what was collected so far")
            summary = benchmark.summarize()
    except RemoteApiError as exc:
        print(f"error: benchmark setup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if teacher_client is not None:
            teacher_client.stop()
        if student_client is not None:
            student_client.stop()
        if created_room_id is not None:
            try:
                teacher_api.close_room(created_room_id)
                print(f"{ROOM_LINE} closed after the run - the relay keeps it, nothing is deleted")
            except RemoteApiError as exc:
                print(f"{ROOM_LINE} warning: could not close the temporary room: {exc}", file=sys.stderr)
        if student_api is not None and student_api.session.authenticated:
            student_api.logout()
        if teacher_api.session.authenticated:
            teacher_api.logout()
        signal.signal(signal.SIGTERM, previous_sigterm)

    if benchmark is None or summary is None:
        return 1
    directory = save_run(benchmark.all_samples(), summary, run_id=args.run_id, base_dir=args.out_dir)
    print(format_summary(summary))
    print(f"saved to {directory}")
    return 0


def _ws_url(server_url: str, room_id: str) -> str:
    base = server_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/ws/v1/rooms/{room_id}"


if __name__ == "__main__":
    raise SystemExit(main())
