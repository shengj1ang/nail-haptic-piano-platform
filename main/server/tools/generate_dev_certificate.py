"""Create a self-signed TLS certificate for local https/wss testing.

    python -m server.tools.generate_dev_certificate --host 127.0.0.1

Writes server/secrets/dev_cert.pem and dev_key.pem. It does NOT enable
TLS: switching a deployment to https/wss is a deliberate edit of
server_config.json's tls block (see doc/server.md), because a
self-signed certificate makes every client warn or refuse until it is
explicitly trusted.

For anything beyond local testing, terminate TLS in front of the relay
with Caddy or Nginx and use a real certificate - the README shows both.

This certificate has nothing to do with the JWT signing keys: it proves
the server's network identity, it does not sign tokens.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    __package__ = "server.tools"

from ..auth import _restrict_permissions  # noqa: E402
from ..config import ServerConfig  # noqa: E402


def _san_entry(host: str):
    from cryptography import x509

    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def main(argv: list[str] | None = None) -> int:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    p = argparse.ArgumentParser(description="Generate a self-signed development TLS certificate")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--host", action="append", default=None, help="hostname or IP (repeatable, default localhost set)")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--certfile", type=Path, default=None)
    p.add_argument("--keyfile", type=Path, default=None)
    args = p.parse_args(argv)

    config = ServerConfig.load(args.config)
    certfile = args.certfile or config.resolve("secrets/dev_cert.pem")
    keyfile = args.keyfile or config.resolve("secrets/dev_key.pem")
    hosts = args.host or ["localhost", "127.0.0.1", "::1"]

    certfile.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hosts[0])])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=args.days))
        .add_extension(x509.SubjectAlternativeName([_san_entry(h) for h in hosts]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    keyfile.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    _restrict_permissions(keyfile)

    print(f"certificate: {certfile}")
    print(f"private key: {keyfile}")
    print(f"valid for {args.days} days, SANs: {', '.join(hosts)}")
    print()
    print("TLS is still OFF. To use it, set in server_config.json:")
    print('  "tls": {"enabled": true, "certfile": "secrets/dev_cert.pem", "keyfile": "secrets/dev_key.pem"}')
    print("and point clients at https:// and wss://.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
