"""What the launcher's Tele-training section actually runs.

The launcher opens every other tool as a sub-window inside its own
process and closes the previous one first, because those tools want
exclusive use of the one camera. The three remote endpoints cannot work
that way: a student, a teacher and a relay have to run *at the same
time*, and the two clients hold different cameras and different MIDI
ports. They are therefore started as independent processes.

Everything here is plain data and plain functions - which command, which
arguments, which working directory, and whether a relay is already
answering - so the launcher's behaviour can be tested without a camera, a
serial port or a running server. The Qt side (LauncherWindow) only turns
a ProcessSpec into a QProcess call.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .config import RemoteGuidanceConfig

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STUDENT_SCRIPT = PROJECT_ROOT / "student_remote_guidance.py"
TEACHER_SCRIPT = PROJECT_ROOT / "teacher_remote_guidance.py"
BENCHMARK_SCRIPT = PROJECT_ROOT / "remote_latency_benchmark.py"


@dataclass(frozen=True)
class ProcessSpec:
    """One launchable process. `program` defaults to the interpreter
    currently running the launcher, so a virtual environment or the
    bundled runtime is inherited rather than guessed at."""

    label: str
    arguments: List[str]
    program: str = field(default_factory=lambda: sys.executable)
    working_directory: Path = PROJECT_ROOT

    def command_line(self) -> str:
        return " ".join([self.program, *self.arguments])

    def missing_script(self) -> Optional[Path]:
        """The script this spec would run, if it is not actually there -
        so the launcher can say which file is missing instead of showing
        a bare non-zero exit code."""
        for argument in self.arguments:
            if argument.endswith(".py"):
                path = Path(argument)
                if not path.is_absolute():
                    path = self.working_directory / path
                return None if path.exists() else path
        return None


def student_spec() -> ProcessSpec:
    return ProcessSpec("Student Client", [str(STUDENT_SCRIPT)])


def teacher_spec() -> ProcessSpec:
    return ProcessSpec("Teacher Client", [str(TEACHER_SCRIPT)])


def benchmark_spec() -> ProcessSpec:
    return ProcessSpec("Network Latency Benchmark", [str(BENCHMARK_SCRIPT), "--gui"])


def server_spec(remote: RemoteGuidanceConfig, gui: Optional[bool] = None) -> ProcessSpec:
    """`python -m server`, with the host/port from the remote config.

    Run from the folder that contains server/, which is also this
    project root - the same invocation a copied-out server uses."""
    local = remote.local_server
    use_gui = local.use_gui if gui is None else gui
    arguments = ["-m", "server", "--host", local.host, "--port", str(local.port)]
    if use_gui:
        arguments.append("--gui")
    return ProcessSpec("Relay Server", arguments)


def health_url(remote: RemoteGuidanceConfig) -> str:
    """Where to check whether a relay is already up.

    Uses the configured server_url when it points at the local relay's
    port, so "is it running?" asks the address the clients will actually
    dial; otherwise the local_server host/port."""
    configured = remote.network.http_base
    if configured and configured.endswith(f":{remote.local_server.port}"):
        return f"{configured}/api/v1/health"
    return f"http://{remote.local_server.host}:{remote.local_server.port}/api/v1/health"


def check_health(url: str, timeout: float = 1.0) -> Optional[dict]:
    """The relay's /health payload, or None if nothing is listening.

    Deliberately a plain urllib GET rather than an import from server/:
    that folder has to stay independently deployable, so the launcher
    talks to it over HTTP like any other client would."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - operator-configured URL
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def server_is_running(remote: RemoteGuidanceConfig, timeout: float = 1.0) -> bool:
    """Used before offering to start a relay: opening a second one on the
    same port would just fail on the bind and show a confusing error."""
    return check_health(health_url(remote), timeout=timeout) is not None
