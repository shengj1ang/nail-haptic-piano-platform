"""Transport latency benchmark for the relayed teacher -> student path.

Defaults follow the report's `System Transmission Performance
Benchmarking`: 1000 simulated key/finger commands at 500 ms intervals
over one already-established WebSocket, with warm-up samples recorded
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

Usage from main/:

    python remote_latency_benchmark.py --server http://127.0.0.1:18765 \\
        --username teacher1 --room-id <room id>

The student client (or --self-test) must be connected to the same room
and answering probes.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import logging
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import RemoteGuidanceConfig
from ..network_client import ClientCallbacks, RemoteApiClient, RemoteApiError, RemoteWebSocketClient
from ..protocol import (
    TYPE_LATENCY_ACK,
    TYPE_LATENCY_PRESENTED,
    TYPE_LATENCY_PROBE,
    TYPE_LATENCY_RECEIVED,
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
    note: int = 60
    finger: str = "R1"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LatencyBenchmark:
    """Drives the probes over an already-connected WebSocket client.

    Takes a client rather than making one, so a test can hand it a stub
    and so a real run reuses the *same* connection a session would - the
    report's benchmark measures one established WebSocket, not repeated
    connection setup."""

    def __init__(self, client: RemoteWebSocketClient, config: Optional[BenchmarkConfig] = None):
        self.client = client
        self.config = config or BenchmarkConfig()
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

    def run(self, progress: Optional[Any] = None) -> Dict[str, Any]:
        total = self.config.warmup + self.config.count
        for i in range(total):
            warmup = i < self.config.warmup
            self._send_probe(seq=i, warmup=warmup)
            if progress is not None:
                progress(i + 1, total)
            time.sleep(self.config.interval_s)

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

    def summarize(self) -> Dict[str, Any]:
        measured = self.measured_samples()
        lost = sum(1 for s in measured if s.lost)
        rtts = [s.rtt_ns for s in measured if s.rtt_ns is not None]

        offset = self.offsets.estimate(synchronised=self.config.clocks_synced)
        one_way = one_way_estimate(
            rtts,
            clock_offset=offset,
            receive_deltas=[s.wall_delta_ns for s in measured if s.wall_delta_ns is not None],
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
            "cue_presented_rtt": summarize_latency(
                presented,
                lost=len(measured) - len(presented) if self.config.trigger_cue else 0,
                label="cue_presented_rtt",
                caveat=(
                    "Includes the student's local cue dispatch. Software dispatch/render timing - "
                    "NOT physical LED or actuator onset."
                ),
            ),
            "student_local_dispatch": summarize_latency(
                [s.student_dispatch_ns for s in measured if s.student_dispatch_ns is not None],
                label="student_local_dispatch",
                caveat=(
                    "Measured on the student's own monotonic clock and reported back as a duration. "
                    "Software dispatch/render timing, not physical cue onset."
                ),
            ),
            "one_way": one_way,
        }

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
            "metrics": {name: stats.as_dict() for name, stats in metrics.items()},
            "notes": [
                "Transport RTT is the primary metric; it needs no clock synchronisation.",
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
        json.dump(summary, f, indent=2)

    _plot(samples, summary, directory / "latency.png")
    return directory


def _plot(samples: List[ProbeSample], summary: Dict[str, Any], path: Path) -> Optional[Path]:
    """Round-trip time over the run plus its distribution. Matplotlib is
    already a platform dependency; a missing backend is not fatal, the
    CSV and JSON are the results."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning("matplotlib not available - skipping the plot")
        return None

    measured = [s for s in samples if not s.warmup and s.rtt_ns is not None]
    if not measured:
        return None
    rtt_ms = [s.rtt_ns / 1e6 for s in measured]
    seqs = [s.seq for s in measured]

    fig, (ax_series, ax_hist) = plt.subplots(1, 2, figsize=(12, 4.5), gridspec_kw={"width_ratios": [2, 1]})
    ax_series.plot(seqs, rtt_ms, linewidth=0.8)
    ax_series.set_xlabel("probe sequence")
    ax_series.set_ylabel("transport RTT (ms)")
    ax_series.set_title("Teacher -> student -> teacher round trip")

    stats = summary.get("metrics", {}).get("transport_rtt", {})
    for label, key, style in (("median", "median_ms", "--"), ("p95", "p95_ms", ":"), ("p99", "p99_ms", "-.")):
        value = stats.get(key)
        if value is not None:
            ax_series.axhline(value, linestyle=style, linewidth=0.9, label=f"{label} {value:.1f} ms")
    ax_series.legend(fontsize=8)

    ax_hist.hist(rtt_ms, bins=40)
    ax_hist.set_xlabel("transport RTT (ms)")
    ax_hist.set_ylabel("probes")
    loss = summary.get("counts", {}).get("loss_rate")
    ax_hist.set_title(f"n={len(rtt_ms)}, loss={0.0 if loss is None else loss * 100:.2f}%")

    fig.suptitle("Software transport timing - not physical cue onset", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


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
        lines.append(
            f"clock offset estimate: {offset['offset_ms']:+.3f} ms "
            f"+/-{offset['uncertainty_ms']:.3f} ms over {offset['samples']} exchanges "
            f"({'NTP-synced asserted' if offset['synchronised'] else 'synchronisation NOT confirmed'})"
        )
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
    p.add_argument("--username", default=remote.network.username, help="teacher account")
    p.add_argument("--password", default=None, help="prompted for if omitted (never stored)")
    p.add_argument("--room-id", default=remote.network.room_id, help="room to probe in")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT, help="measured probes")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S, help="seconds between probes")
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

    if not args.room_id:
        print("error: --room-id is required (create or join a room in the teacher client first)", file=sys.stderr)
        return 2

    api = RemoteApiClient(args.server, verify_tls=not args.no_verify_tls)
    password = args.password or getpass.getpass(f"password for {args.username}: ")
    try:
        session = api.login(args.username, password)
    except RemoteApiError as exc:
        print(f"error: could not sign in: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""  # not kept around after the one call that needs it

    ws_url = _ws_url(args.server, args.room_id)
    config = BenchmarkConfig(
        count=args.count,
        interval_s=args.interval,
        warmup=args.warmup,
        ack_timeout_s=args.timeout,
        trigger_cue=args.trigger_cue,
        clocks_synced=args.clocks_synced,
    )

    client = RemoteWebSocketClient(
        ws_url=ws_url,
        access_token=session.access_token,
        room_id=args.room_id,
        verify_tls=not args.no_verify_tls,
        auto_reconnect=False,  # a reconnect mid-run would invalidate the sample
    )
    benchmark = LatencyBenchmark(client, config)
    client.callbacks = ClientCallbacks(on_message=benchmark.on_message)
    client.start()

    if not client.wait_until_connected(timeout=20.0):
        print("error: could not connect to the room's WebSocket", file=sys.stderr)
        client.stop()
        return 1

    total = config.warmup + config.count
    print(
        f"probing: {config.count} measured + {config.warmup} warm-up at {config.interval_s * 1000:.0f} ms "
        f"intervals over one established WebSocket (~{total * config.interval_s / 60:.1f} min)"
    )
    if not args.clocks_synced:
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
    finally:
        client.stop()

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
