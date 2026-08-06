"""server_config.json - the only file an operator edits to deploy this.

Every path in it is resolved relative to the server folder itself, so a
copied-out server/ keeps working wherever it lands (see server/README.md).
Secrets are deliberately not in here as values: the config only points at
the JWT key *files*, which server/tools/generate_keys.py creates outside
version control.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SERVER_DIR / "server_config.json"
EXAMPLE_CONFIG_PATH = SERVER_DIR / "server_config.example.json"
SECRETS_DIR = SERVER_DIR / "secrets"

# Bigger than any legitimate envelope (a guidance event with a full
# ten-finger probability distribution is well under 2 KiB); anything past
# this is rejected before it is parsed.
MAX_MESSAGE_BYTES = 65536


@dataclass
class TlsConfig:
    """Transport security for the HTTP/WebSocket listener.

    Completely separate from the JWT signing keys: this X.509
    certificate/key pair proves the server's identity to a browser or
    client socket, while the RSA pair in secrets/ signs access tokens.
    Reusing one for the other is a mistake the two config blocks are
    named apart to prevent."""

    enabled: bool = False
    certfile: Optional[str] = None
    keyfile: Optional[str] = None


@dataclass
class ServerConfig:
    bind_host: str = "127.0.0.1"
    # Kept in step with remote_guidance.config.LocalServerConfig.port and
    # NetworkConfig.server_url - the clients' default address is this
    # port, so the three move together or nothing connects.
    port: int = 18765
    database_path: str = "data/remote_guidance.db"
    jwt_private_key_path: str = "secrets/jwt_private.pem"
    jwt_public_key_path: str = "secrets/jwt_public.pem"
    # Long by request: a teacher/student pair should not be logged out
    # part-way through a term. Both are configurable, and logout/
    # revocation (see auth.revoke_token) is the way to end a session early.
    access_token_ttl_days: int = 30
    refresh_token_ttl_days: int = 180
    issuer: str = "remote-guidance-server"
    audience: str = "remote-guidance-clients"
    allow_registration: bool = True
    tls: TlsConfig = field(default_factory=TlsConfig)

    # Where this config was loaded from - used to resolve relative paths.
    source_path: Optional[Path] = None

    # ------------------------------------------------------------------

    def _base_dir(self) -> Path:
        return self.source_path.parent if self.source_path else SERVER_DIR

    def resolve(self, relative: str) -> Path:
        p = Path(relative).expanduser()
        return p if p.is_absolute() else (self._base_dir() / p)

    @property
    def database_file(self) -> Path:
        return self.resolve(self.database_path)

    @property
    def jwt_private_key_file(self) -> Path:
        return self.resolve(self.jwt_private_key_path)

    @property
    def jwt_public_key_file(self) -> Path:
        return self.resolve(self.jwt_public_key_path)

    @property
    def tls_certfile(self) -> Optional[Path]:
        return self.resolve(self.tls.certfile) if self.tls.certfile else None

    @property
    def tls_keyfile(self) -> Optional[Path]:
        return self.resolve(self.tls.keyfile) if self.tls.keyfile else None

    @property
    def scheme(self) -> str:
        return "https" if self.tls.enabled else "http"

    @property
    def ws_scheme(self) -> str:
        return "wss" if self.tls.enabled else "ws"

    def http_base_url(self, host: Optional[str] = None) -> str:
        return f"{self.scheme}://{host or self._display_host()}:{self.port}"

    def ws_base_url(self, host: Optional[str] = None) -> str:
        return f"{self.ws_scheme}://{host or self._display_host()}:{self.port}"

    def _display_host(self) -> str:
        # 0.0.0.0 is a bind directive, not an address a client can dial.
        return "127.0.0.1" if self.bind_host in ("0.0.0.0", "::", "") else self.bind_host

    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Dict[str, Any], source_path: Optional[Path] = None) -> "ServerConfig":
        tls_data = data.get("tls") or {}
        known = {f for f in cls.__dataclass_fields__ if f not in ("tls", "source_path")}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(
            **kwargs,
            tls=TlsConfig(
                enabled=bool(tls_data.get("enabled", False)),
                certfile=tls_data.get("certfile"),
                keyfile=tls_data.get("keyfile"),
            ),
            source_path=source_path,
        )

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ServerConfig":
        """Missing file = built-in defaults (local development, no TLS),
        so a fresh checkout runs without an operator writing any config."""
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not path.exists():
            return cls(source_path=path)
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f), source_path=path)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data.pop("source_path", None)
        return data

    def save(self, path: Optional[Path] = None) -> None:
        path = Path(path) if path else (self.source_path or DEFAULT_CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        tmp.replace(path)
