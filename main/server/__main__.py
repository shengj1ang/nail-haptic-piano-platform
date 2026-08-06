"""Command line entry point.

    python -m server                      # bind from server_config.json
    python -m server --gui                # Qt control panel instead
    python -m server --host 0.0.0.0 --port 18765
    python -m server --init               # make keys/config, then exit

Run it from the folder that *contains* server/ - that is the working
directory in this repo (main/), and it is also all a remote host needs
after copying the folder across (see server/README.md).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow `python server/__main__.py` as well as `python -m server`, which
# is the form a copied-out folder is most likely to be started with.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "server"

from .config import DEFAULT_CONFIG_PATH, EXAMPLE_CONFIG_PATH, ServerConfig  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m server", description="Remote Guidance relay server")
    p.add_argument("--config", type=Path, default=None, help=f"config file (default: {DEFAULT_CONFIG_PATH.name})")
    p.add_argument("--host", default=None, help="override bind_host")
    p.add_argument("--port", type=int, default=None, help="override port")
    p.add_argument("--database", type=Path, default=None, help="override database_path")
    p.add_argument("--gui", action="store_true", help="open the Qt control panel instead of serving directly")
    p.add_argument("--init", action="store_true", help="create server_config.json and the JWT key pair, then exit")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    return p


def load_config(args: argparse.Namespace) -> ServerConfig:
    config = ServerConfig.load(args.config)
    if args.host:
        config.bind_host = args.host
    if args.port:
        config.port = int(args.port)
    if args.database:
        config.database_path = str(args.database)
    return config


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .app import configure_logging

    configure_logging(logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args)

    if args.init:
        return _init(config)

    if args.gui:
        from .gui import run_gui

        return run_gui(config, config_path=args.config)

    import uvicorn

    from .app import build_uvicorn_kwargs, create_app

    app = create_app(config)
    uvicorn.run(app, **build_uvicorn_kwargs(config))
    return 0


def _init(config: ServerConfig) -> int:
    from .auth import TokenService
    from .database import Database

    target = config.source_path or DEFAULT_CONFIG_PATH
    if not target.exists():
        config.save(target)
        print(f"wrote {target}")
    else:
        print(f"{target} already exists - left untouched")
        if EXAMPLE_CONFIG_PATH.exists():
            print(f"(reference copy: {EXAMPLE_CONFIG_PATH})")

    db = Database(config.database_file)
    print(f"database ready at {config.database_file} (schema v{db.schema_version})")

    tokens = TokenService(config, db)
    tokens.ensure_keys()
    print(f"JWT signing keys: {config.jwt_private_key_file} / {config.jwt_public_key_file}")
    print("These sign tokens. They are NOT a TLS certificate - see tools/generate_dev_certificate.py for that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
