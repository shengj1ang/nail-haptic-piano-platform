"""Create the RSA pair that signs JWT access/refresh tokens.

    python -m server.tools.generate_keys

Writes server/secrets/jwt_private.pem and jwt_public.pem (paths come from
server_config.json), private key mode 0600 where the platform supports
it. secrets/ is git-ignored.

This is NOT the TLS certificate. TLS proves the server's network identity
to a client socket and uses a separate X.509 pair - see
generate_dev_certificate.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    __package__ = "server.tools"

from ..auth import AuthError, generate_keypair  # noqa: E402
from ..config import ServerConfig  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the JWT signing key pair")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--key-size", type=int, default=2048)
    p.add_argument("--force", action="store_true", help="rotate: delete existing keys first (invalidates all tokens)")
    args = p.parse_args(argv)

    config = ServerConfig.load(args.config)
    private, public = config.jwt_private_key_file, config.jwt_public_key_file

    if args.force:
        for path in (private, public):
            if path.exists():
                path.unlink()
                print(f"removed {path}")

    try:
        generate_keypair(private, public, key_size=args.key_size)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"private key: {private}")
    print(f"public key:  {public}")
    print("Keep secrets/ out of version control and off shared storage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
