"""JWT signing/verification and password hashing for auth-service.

Signing keys are loaded once per unique key configuration and cached. The
public key is derived from the private key when no explicit public key is
configured, and ``kid`` is the RFC 7638 JWK thumbprint so verifiers can match
keys by their canonical public JWK.
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings, get_settings

ALGORITHM = "RS256"

_JSON_SEPARATORS = (",", ":")


class TokenError(Exception):
    """Base class for access-token failures."""


class TokenExpiredError(TokenError):
    """Raised when an access token's ``exp`` claim has passed."""


class TokenInvalidError(TokenError):
    """Raised when an access token is malformed, unsigned or otherwise invalid."""


@dataclass(frozen=True)
class SigningKeys:
    """Parsed RSA key material and the derived key id."""

    private_key: rsa.RSAPrivateKey
    public_key: rsa.RSAPublicKey
    kid: str


@dataclass(frozen=True)
class TokenClaims:
    """Verified claims carried by an access token."""

    sub: str
    email: str
    iat: int
    exp: int
    iss: str
    aud: str
    jti: str


_SIGNING_KEYS_CACHE: dict[tuple[str, str, str, str], SigningKeys] = {}


def _cache_key(settings: Settings) -> tuple[str, str, str, str]:
    """Return a stable cache key for the configured key sources."""
    return (
        settings.auth_jwt_private_key_file or "",
        settings.auth_jwt_private_key or "",
        settings.auth_jwt_public_key_file or "",
        settings.auth_jwt_public_key or "",
    )


def _coerce_pem(value: str) -> str:
    """Turn literal ``\\n`` escapes from env vars into real newlines."""
    return value.replace("\\n", "\n")


def _read_pem_file(path: str, env_name: str) -> str:
    """Read a PEM file, converting filesystem errors into a clear RuntimeError."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"{env_name} could not be read: {path}") from exc


def _private_pem(settings: Settings) -> str:
    """Resolve the PEM-encoded private key, failing fast when absent."""
    if settings.auth_jwt_private_key_file:
        return _read_pem_file(settings.auth_jwt_private_key_file, "AUTH_JWT_PRIVATE_KEY_FILE")
    if settings.auth_jwt_private_key:
        return _coerce_pem(settings.auth_jwt_private_key)
    raise RuntimeError(
        "No JWT signing key configured: set AUTH_JWT_PRIVATE_KEY or AUTH_JWT_PRIVATE_KEY_FILE"
    )


def _public_pem(settings: Settings) -> str | None:
    """Resolve the optional PEM-encoded public key."""
    if settings.auth_jwt_public_key_file:
        return _read_pem_file(settings.auth_jwt_public_key_file, "AUTH_JWT_PUBLIC_KEY_FILE")
    if settings.auth_jwt_public_key:
        return _coerce_pem(settings.auth_jwt_public_key)
    return None


def _load_private_key(pem: str) -> rsa.RSAPrivateKey:
    """Parse and type-check a PEM private key."""
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("AUTH_JWT_PRIVATE_KEY is not a valid PEM private key") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError("AUTH_JWT_PRIVATE_KEY must be an RSA private key")
    return key


def _load_public_key(pem: str) -> rsa.RSAPublicKey:
    """Parse and type-check a PEM public key."""
    try:
        key = serialization.load_pem_public_key(pem.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("AUTH_JWT_PUBLIC_KEY is not a valid PEM public key") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise RuntimeError("AUTH_JWT_PUBLIC_KEY must be an RSA public key")
    return key


def _b64url_uint(value: int) -> str:
    """Encode a non-negative integer as an unpadded base64url string."""
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_jwk(public_key: rsa.RSAPublicKey) -> dict[str, str]:
    """Return the canonical (lexicographically ordered) RSA public JWK."""
    numbers = public_key.public_numbers()
    return {"e": _b64url_uint(numbers.e), "kty": "RSA", "n": _b64url_uint(numbers.n)}


def _thumbprint(public_key: rsa.RSAPublicKey) -> str:
    """Return the RFC 7638 JWK thumbprint of ``public_key``."""
    canonical = json.dumps(_public_jwk(public_key), separators=_JSON_SEPARATORS)
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def load_signing_keys(settings: Settings) -> SigningKeys:
    """Load and cache signing keys for ``settings``.

    Prefers ``auth_jwt_private_key_file`` over the inline key and falls back to
    ``auth_jwt_private_key``. Raises :class:`RuntimeError` naming the relevant
    environment variable when no usable private key is configured.
    """
    cache_key = _cache_key(settings)
    cached = _SIGNING_KEYS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    private_key = _load_private_key(_private_pem(settings))
    public_pem = _public_pem(settings)
    public_key = (
        _load_public_key(public_pem) if public_pem is not None else private_key.public_key()
    )
    signing_keys = SigningKeys(
        private_key=private_key,
        public_key=public_key,
        kid=_thumbprint(public_key),
    )
    _SIGNING_KEYS_CACHE[cache_key] = signing_keys
    return signing_keys


def build_jwks(
    *,
    settings: Settings | None = None,
    keys: SigningKeys | None = None,
) -> dict[str, Any]:
    """Return the public JWKS document for the signing key."""
    signing = keys or load_signing_keys(settings or get_settings())
    jwk = _public_jwk(signing.public_key)
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": ALGORITHM,
                "kid": signing.kid,
                "n": jwk["n"],
                "e": jwk["e"],
            }
        ]
    }


def create_access_token(
    user_id: uuid.UUID | str,
    email: str,
    *,
    settings: Settings | None = None,
    keys: SigningKeys | None = None,
) -> str:
    """Sign an RS256 access token for ``user_id`` with an ``email`` claim."""
    resolved = settings or get_settings()
    signing = keys or load_signing_keys(resolved)
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=resolved.auth_access_token_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": resolved.auth_jwt_issuer,
        "aud": resolved.auth_jwt_audience,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(
        payload,
        signing.private_key,
        algorithm=ALGORITHM,
        headers={"kid": signing.kid},
    )


def decode_access_token(
    token: str,
    *,
    settings: Settings | None = None,
    keys: SigningKeys | None = None,
) -> TokenClaims:
    """Verify ``token`` and return its claims.

    Distinguishes expiry (:class:`TokenExpiredError`) from any other failure
    (:class:`TokenInvalidError`), and enforces the configured issuer/audience.
    """
    resolved = settings or get_settings()
    signing = keys or load_signing_keys(resolved)
    try:
        payload = jwt.decode(
            token,
            signing.public_key,
            algorithms=[ALGORITHM],
            issuer=resolved.auth_jwt_issuer,
            audience=resolved.auth_jwt_audience,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalidError("access token is invalid") from exc

    return TokenClaims(
        sub=str(payload["sub"]),
        email=str(payload.get("email", "")),
        iat=int(payload["iat"]),
        exp=int(payload["exp"]),
        iss=str(payload.get("iss", "")),
        aud=str(payload.get("aud", "")),
        jti=str(payload.get("jti", "")),
    )


_hasher = PasswordHasher()
_DUMMY_PASSWORD = "kubecommerce-dummy-password-not-used-for-login"
DUMMY_PASSWORD_HASH = _hasher.hash(_DUMMY_PASSWORD)


def hash_password(password: str) -> str:
    """Hash ``password`` with argon2id and the library defaults."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return whether ``password`` matches ``password_hash`` (never raises)."""
    try:
        return _hasher.verify(password_hash, password)
    except (Argon2Error, InvalidHashError):
        # InvalidHashError subclasses ValueError, not Argon2Error; a corrupted
        # stored hash must fail closed (return False) instead of raising a 500.
        return False


def needs_rehash(password_hash: str) -> bool:
    """Return whether ``password_hash`` uses outdated argon2 parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (Argon2Error, InvalidHashError):
        # An unparseable hash must be treated as needing a rehash.
        return True
