"""Network latency benchmark for the relayed tele-training path.

By default it logs in as both a teacher and a built-in simulated student,
creates a temporary room, opens both WebSockets and reports what came
back. Only this process and the Relay Server are required. Defaults follow
the report's `System Transmission Performance Benchmarking`: 1000 probes
at 500 ms intervals, with warm-up samples recorded separately and excluded
from the statistics.

    python remote_latency_benchmark.py --server http://127.0.0.1:18765 \
        --username teacher1 --student-username student1

    python remote_latency_benchmark.py --gui

Round-trip time is the primary metric, because both of its timestamps are
taken from one monotonic clock on this machine and it therefore needs no
clock synchronisation. One-way latency is only reported as a measurement
when --clocks-synced asserts both hosts are NTP-synchronised and the
offset uncertainty is recorded with it; otherwise the output shows RTT/2,
labelled a symmetry-based estimate.

Built-in mode measures the network/relay route and does not claim Student
UI, LED, haptic or physical onset timing. Use `--external-student` with a
real Student Client and room id only when that real software/hardware path
is the object of the test - see doc/server.md.

Results land in data/remote_guidance/latency/<run id>/ as samples.csv,
summary.json and latency.png.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    argv = sys.argv[1:]
    if "--gui" in argv:
        from remote_guidance.tools.benchmark_window import main as gui_main

        return gui_main()

    from remote_guidance.tools.latency_benchmark import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
