"""Unit tests for key loading, password hashing and JWT handling."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import Settings
from app.security import (
    ALGORITHM,
    DUMMY_PASSWORD_HASH,
    TokenExpiredError,
    TokenInvalidError,
    build_jwks,
    create_access_token,
    decode_access_token,
    hash_password,
    load_signing_keys,
    needs_rehash,
    verify_password,
)


def _settings(private_pem: str | None, **overrides: object) -> Settings:
    """Build a Settings instance with a SQLite URL and the given keys."""
    values: dict[str, object] = {
        "auth_database_url": "sqlite+aiosqlite://",
        "auth_jwt_private_key": private_pem,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _expected_thumbprint(jwk: dict[str, str]) -> str:
    """Independently compute the RFC 7638 thumbprint of an RSA public JWK."""
    canonical = json.dumps(
        {"e": jwk["e"], "kty": jwk["kty"], "n": jwk["n"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _hs256_token(settings: Settings) -> str:
    """Sign an otherwise-valid payload with HS256 to exercise algorithm checks."""
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "alg@example.com",
        "iat": int((now - timedelta(seconds=1)).timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.auth_jwt_issuer,
        "aud": settings.auth_jwt_audience,
        "jti": str(uuid.uuid4()),
    }
    # Decoy key is >=32 bytes so PyJWT's InsecureKeyLengthWarning is not emitted.
    return jwt.encode(payload, "decoy-hmac-key-for-negative-tests-0123456789", algorithm="HS256")


# --- key loading -------------------------------------------------------------


def test_inline_and_escaped_newlines_are_equivalent(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    inline = load_signing_keys(_settings(private_pem))
    escaped = load_signing_keys(_settings(private_pem.replace("\n", "\\n")))
    assert inline.kid == escaped.kid
    assert inline.public_key.public_numbers() == escaped.public_key.public_numbers()


def test_public_key_is_derived_when_absent(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    keys = load_signing_keys(_settings(private_pem))
    expected = serialization.load_pem_private_key(private_pem.encode("utf-8"), None).public_key()
    assert keys.public_key.public_numbers() == expected.public_numbers()


def test_load_from_files(tmp_path: Path, rsa_pem: tuple[str, str]) -> None:
    private_pem, public_pem = rsa_pem
    private_path = tmp_path / "private.pem"
    public_path = tmp_path / "public.pem"
    private_path.write_text(private_pem, encoding="utf-8")
    public_path.write_text(public_pem, encoding="utf-8")
    keys = load_signing_keys(
        _settings(
            None,
            auth_jwt_private_key_file=str(private_path),
            auth_jwt_public_key_file=str(public_path),
        )
    )
    assert keys.kid == load_signing_keys(_settings(private_pem)).kid


def test_private_file_derives_public_key(tmp_path: Path, rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    private_path = tmp_path / "private.pem"
    private_path.write_text(private_pem, encoding="utf-8")
    keys = load_signing_keys(_settings(None, auth_jwt_private_key_file=str(private_path)))
    assert keys.public_key is not None
    assert keys.kid


def test_missing_private_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_JWT_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("AUTH_JWT_PRIVATE_KEY_FILE", raising=False)
    settings = Settings(_env_file=None, auth_database_url="sqlite+aiosqlite://")
    with pytest.raises(RuntimeError, match="AUTH_JWT_PRIVATE_KEY"):
        load_signing_keys(settings)


def test_missing_private_key_file_fails_fast(tmp_path: Path) -> None:
    settings = _settings(None, auth_jwt_private_key_file=str(tmp_path / "missing.pem"))
    with pytest.raises(RuntimeError, match="AUTH_JWT_PRIVATE_KEY_FILE"):
        load_signing_keys(settings)


def test_invalid_private_pem_fails_fast() -> None:
    with pytest.raises(RuntimeError, match="not a valid PEM private key"):
        load_signing_keys(_settings("this-is-not-a-pem"))


def test_non_rsa_private_key_fails_fast() -> None:
    ec_private_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode("ascii")
    )
    with pytest.raises(RuntimeError, match="must be an RSA private key"):
        load_signing_keys(_settings(ec_private_pem))


def test_invalid_public_pem_fails_fast(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    with pytest.raises(RuntimeError, match="not a valid PEM public key"):
        load_signing_keys(_settings(private_pem, auth_jwt_public_key="not-a-pem"))


def test_non_rsa_public_key_fails_fast(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    ec_public_pem = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    with pytest.raises(RuntimeError, match="must be an RSA public key"):
        load_signing_keys(_settings(private_pem, auth_jwt_public_key=ec_public_pem))


# --- JWKS / kid --------------------------------------------------------------


def test_kid_is_rfc7638_thumbprint(rsa_pem: tuple[str, str]) -> None:
    keys = load_signing_keys(_settings(rsa_pem[0]))
    jwk = build_jwks(keys=keys)["keys"][0]
    assert keys.kid == _expected_thumbprint(jwk)
    assert jwk["kid"] == keys.kid


def test_kid_is_stable_across_loads(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    first = load_signing_keys(_settings(private_pem))
    second = load_signing_keys(_settings(private_pem))
    assert first.kid == second.kid


def test_jwks_shape(rsa_pem: tuple[str, str]) -> None:
    keys = load_signing_keys(_settings(rsa_pem[0]))
    document = build_jwks(keys=keys)
    assert list(document) == ["keys"]
    jwk = document["keys"][0]
    assert set(jwk) == {"kty", "use", "alg", "kid", "n", "e"}
    assert jwk["kty"] == "RSA"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == ALGORITHM
    assert jwk["n"] and jwk["e"]


def test_jwks_verifies_issued_token(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    keys = load_signing_keys(settings)
    token = create_access_token(uuid.uuid4(), "verify@example.com", settings=settings, keys=keys)
    jwk = build_jwks(keys=keys)["keys"][0]
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    payload = jwt.decode(
        token,
        public_key,
        algorithms=[ALGORITHM],
        issuer=settings.auth_jwt_issuer,
        audience=settings.auth_jwt_audience,
    )
    assert payload["email"] == "verify@example.com"
    assert payload["sub"]


# --- password hashing --------------------------------------------------------


def test_password_hash_verify_and_rehash() -> None:
    password = "correct-horse-battery-staple"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrong-password", hashed) is False
    assert needs_rehash(hashed) is False


def test_malformed_hash_fails_closed() -> None:
    # InvalidHashError subclasses ValueError (not Argon2Error); both helpers must
    # tolerate a corrupted stored hash instead of raising.
    assert verify_password("whatever", "not-a-valid-hash") is False
    assert needs_rehash("not-a-valid-hash") is True


def test_dummy_hash_is_a_real_argon2_hash() -> None:
    assert verify_password("anything", DUMMY_PASSWORD_HASH) is False
    assert needs_rehash(DUMMY_PASSWORD_HASH) is False


# --- tokens ------------------------------------------------------------------


def test_token_round_trip(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    keys = load_signing_keys(settings)
    user_id = uuid.uuid4()
    token = create_access_token(user_id, "roundtrip@example.com", settings=settings, keys=keys)
    claims = decode_access_token(token, settings=settings, keys=keys)
    assert claims.sub == str(user_id)
    assert claims.email == "roundtrip@example.com"
    assert claims.iss == settings.auth_jwt_issuer
    assert claims.aud == settings.auth_jwt_audience
    assert claims.exp > claims.iat
    assert claims.jti


def test_expired_token_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    expired_settings = settings.model_copy(update={"auth_access_token_ttl_seconds": -10})
    keys = load_signing_keys(settings)
    token = create_access_token(
        uuid.uuid4(), "expired@example.com", settings=expired_settings, keys=keys
    )
    with pytest.raises(TokenExpiredError):
        decode_access_token(token, settings=settings, keys=keys)


def test_wrong_audience_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    signer = _settings(private_pem, auth_jwt_audience="kubecommerce")
    verifier = _settings(private_pem, auth_jwt_audience="other-audience")
    keys = load_signing_keys(signer)
    token = create_access_token(uuid.uuid4(), "aud@example.com", settings=signer, keys=keys)
    with pytest.raises(TokenInvalidError):
        decode_access_token(token, settings=verifier, keys=keys)


def test_wrong_issuer_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    signer = _settings(private_pem, auth_jwt_issuer="kubecommerce-auth")
    verifier = _settings(private_pem, auth_jwt_issuer="someone-else")
    keys = load_signing_keys(signer)
    token = create_access_token(uuid.uuid4(), "iss@example.com", settings=signer, keys=keys)
    with pytest.raises(TokenInvalidError):
        decode_access_token(token, settings=verifier, keys=keys)


def test_wrong_algorithm_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    keys = load_signing_keys(settings)
    with pytest.raises(TokenInvalidError):
        decode_access_token(_hs256_token(settings), settings=settings, keys=keys)


def test_tampered_signature_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    keys = load_signing_keys(settings)
    token = create_access_token(uuid.uuid4(), "tamper@example.com", settings=settings, keys=keys)
    header, payload, signature = token.split(".")
    replacement = "A" if signature[-1] != "A" else "B"
    tampered = f"{header}.{payload}.{signature[:-1]}{replacement}"
    with pytest.raises(TokenInvalidError):
        decode_access_token(tampered, settings=settings, keys=keys)


def test_unsigned_token_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    keys = load_signing_keys(settings)
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "email": "none@example.com",
        "iat": int((now - timedelta(seconds=1)).timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.auth_jwt_issuer,
        "aud": settings.auth_jwt_audience,
    }
    unsigned = jwt.encode(payload, None, algorithm="none")
    with pytest.raises(TokenInvalidError):
        decode_access_token(unsigned, settings=settings, keys=keys)


def test_token_missing_required_claims_is_rejected(rsa_pem: tuple[str, str]) -> None:
    private_pem, _ = rsa_pem
    settings = _settings(private_pem)
    keys = load_signing_keys(settings)
    now = datetime.now(UTC)
    payload = {
        "email": "nosub@example.com",
        "iat": int((now - timedelta(seconds=1)).timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.auth_jwt_issuer,
        "aud": settings.auth_jwt_audience,
    }
    token = jwt.encode(payload, keys.private_key, algorithm=ALGORITHM)
    with pytest.raises(TokenInvalidError):
        decode_access_token(token, settings=settings, keys=keys)
