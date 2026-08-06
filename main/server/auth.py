"""Passwords, tokens and the two key pairs people confuse.

Passwords are stored as Argon2id hashes (memory-hard, salted per user);
the plaintext is never written anywhere, including logs.

Access/refresh tokens are RS256 JWTs signed with the RSA private key in
secrets/jwt_private.pem and verified with secrets/jwt_public.pem. That
pair signs *tokens*. It is NOT the TLS certificate: TLS uses a separate
X.509 certificate/key that proves the server's network identity to a
client socket (see server/config.py's TlsConfig and
tools/generate_dev_certificate.py). Neither can substitute for the other,
and a deployment normally has both.

Long sessions are a deliberate requirement here (30-day access, 180-day
refresh by default), so revocation is not optional: every token carries a
jti, logout writes it to revoked_tokens, and verification rejects a
revoked jti even while its exp is still in the future.
"""

from __future__ import annotations

import datetime as dt
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

from .config import ServerConfig
from .database import Database, is_token_revoked, revoke_token

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"
ALGORITHM = "RS256"

_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 8


class AuthError(Exception):
    """Anything wrong with a credential or a token. The message is safe to
    return to a client - it never contains the password or the token."""


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHash:
        return False


# ---------------------------------------------------------------------------
# RSA key material
# ---------------------------------------------------------------------------


def generate_keypair(private_path: Path, public_path: Path, key_size: int = 2048) -> Tuple[Path, Path]:
    """Write a fresh RSA pair for JWT signing, private key readable only by
    the current user. Never overwrites an existing private key - rotating
    invalidates every issued token, so it has to be a deliberate delete."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_path = Path(private_path)
    public_path = Path(public_path)
    if private_path.exists():
        raise AuthError(f"{private_path} already exists - delete it explicitly to rotate the signing key")

    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)
    _restrict_permissions(private_path)
    return private_path, public_path


def _restrict_permissions(path: Path) -> None:
    """0600 where the platform honours it. Windows ignores POSIX mode bits,
    so this is best-effort by design - the README says to keep secrets/ off
    shared storage regardless."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):
        pass


@dataclass
class TokenClaims:
    sub: str
    role: str
    jti: str
    token_type: str
    exp: float
    iat: float
    raw: Dict[str, Any]


class TokenService:
    """Issues and verifies the two token types against one RSA pair."""

    def __init__(self, config: ServerConfig, db: Database):
        self.config = config
        self.db = db
        self._private_key: Optional[bytes] = None
        self._public_key: Optional[bytes] = None

    # -- keys ----------------------------------------------------------

    def ensure_keys(self) -> None:
        """Create the signing pair on first run so `python -m server` works
        out of the box; an operator can also run tools/generate_keys.py
        ahead of time."""
        private = self.config.jwt_private_key_file
        public = self.config.jwt_public_key_file
        if not private.exists() or not public.exists():
            if private.exists() != public.exists():
                raise AuthError(
                    f"JWT key pair is incomplete ({private} / {public}) - delete the remaining file or "
                    "restore its partner before starting"
                )
            generate_keypair(private, public)
        else:
            _restrict_permissions(private)

    @property
    def private_key(self) -> bytes:
        if self._private_key is None:
            self._private_key = self.config.jwt_private_key_file.read_bytes()
        return self._private_key

    @property
    def public_key(self) -> bytes:
        if self._public_key is None:
            self._public_key = self.config.jwt_public_key_file.read_bytes()
        return self._public_key

    # -- issuing -------------------------------------------------------

    def _issue(self, user_id: str, role: str, token_type: str, ttl_days: int) -> Tuple[str, Dict[str, Any]]:
        now = dt.datetime.now(dt.timezone.utc)
        exp = now + dt.timedelta(days=ttl_days)
        claims = {
            "sub": user_id,
            "role": role,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "jti": str(uuid.uuid4()),
            "iss": self.config.issuer,
            "aud": self.config.audience,
            "typ": token_type,
        }
        return jwt.encode(claims, self.private_key, algorithm=ALGORITHM), claims

    def issue_pair(self, user_id: str, role: str) -> Dict[str, Any]:
        access, access_claims = self._issue(user_id, role, TOKEN_TYPE_ACCESS, self.config.access_token_ttl_days)
        refresh, refresh_claims = self._issue(user_id, role, TOKEN_TYPE_REFRESH, self.config.refresh_token_ttl_days)
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_at": access_claims["exp"],
            "refresh_expires_at": refresh_claims["exp"],
            "access_jti": access_claims["jti"],
            "refresh_jti": refresh_claims["jti"],
        }

    # -- verifying -----------------------------------------------------

    def verify(self, token: str, expected_type: str = TOKEN_TYPE_ACCESS) -> TokenClaims:
        try:
            payload = jwt.decode(
                token,
                self.public_key,
                algorithms=[ALGORITHM],
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["exp", "iat", "nbf", "sub", "jti", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthError("token expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthError(f"invalid token: {exc}") from exc

        if payload.get("typ") != expected_type:
            raise AuthError(f"expected a {expected_type} token")
        if is_token_revoked(self.db, payload["jti"]):
            raise AuthError("token has been revoked")

        return TokenClaims(
            sub=payload["sub"],
            role=payload.get("role", ""),
            jti=payload["jti"],
            token_type=payload.get("typ", ""),
            exp=float(payload["exp"]),
            iat=float(payload["iat"]),
            raw=payload,
        )

    def revoke(self, token: str, expected_type: str = TOKEN_TYPE_ACCESS) -> None:
        """Revoking an already-expired or already-revoked token is a no-op
        rather than an error: logging out twice must still succeed."""
        try:
            claims = self.verify(token, expected_type=expected_type)
        except AuthError:
            unverified = self._peek(token)
            if unverified is None:
                raise
            revoke_token(self.db, unverified["jti"], unverified.get("sub", ""), float(unverified.get("exp", 0)))
            return
        revoke_token(self.db, claims.jti, claims.sub, claims.exp)

    def _peek(self, token: str) -> Optional[Dict[str, Any]]:
        """Claims without signature/expiry checks - only ever used to find
        the jti of a token we are about to blacklist anyway."""
        try:
            return jwt.decode(token, options={"verify_signature": False, "verify_exp": False, "verify_aud": False})
        except jwt.InvalidTokenError:
            return None


def redact_token(token: Optional[str]) -> str:
    """What may appear in a log line. Never the token itself."""
    if not token:
        return "<none>"
    return f"<token len={len(token)} ...{token[-4:]}>"
