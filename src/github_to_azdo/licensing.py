"""Offline license verification for ``gh-to-azdo batch``.

Licenses are offline, signed annual "Pro batch" entitlements delivered
manually (invoice + payment link handled outside this tool).  Verification is
fail-closed: the trusted Ed25519 public key is baked into the build via the
``PRO_PUBLIC_KEY_B64`` module constant and is never read from the license file
or the environment.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

# Vendor provisioning point: replace with the standard-base64 raw Ed25519
# public key (32 bytes -> 44 chars) before building the distributed wheel.
# An empty value makes every license verification fail closed.
PRO_PUBLIC_KEY_B64 = ""

_MAX_LICENSE_BYTES = 16 * 1024
_PAYLOAD_KEYS = frozenset({"version", "customer", "expires", "features"})
_ENVELOPE_KEYS = frozenset({"payload", "signature"})


class LicenseError(ValueError):
    """Raised when a license file is missing, malformed, or not trusted."""


def canonical_payload(payload: dict[str, Any]) -> bytes:
    """Canonical JSON bytes that the Ed25519 signature covers."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _fail(message: str) -> None:
    raise LicenseError(message)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            _fail("license contains duplicate keys")
        obj[key] = value
    return obj


def _read_license(path: Path) -> bytes:
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_LICENSE_BYTES + 1)
    except OSError as exc:
        _fail(f"cannot read license file: {exc.strerror or exc}")
    if len(data) > _MAX_LICENSE_BYTES:
        _fail("license file is too large")
    return data


def _trusted_public_key() -> Ed25519PublicKey:
    if not PRO_PUBLIC_KEY_B64:
        _fail("no trusted license public key is configured in this build")
    try:
        key_bytes = base64.b64decode(PRO_PUBLIC_KEY_B64, validate=True)
        return Ed25519PublicKey.from_public_bytes(key_bytes)
    except (binascii.Error, ValueError):
        _fail("configured license public key is invalid")


def _validate_payload(payload: Any) -> tuple[dict[str, Any], date]:
    if not isinstance(payload, dict):
        _fail("license payload must be a JSON object")
    if set(payload.keys()) != _PAYLOAD_KEYS:
        _fail("license payload has missing or unexpected fields")

    version = payload["version"]
    if type(version) is not int or version != 1:
        _fail("unsupported license version")

    customer = payload["customer"]
    if not isinstance(customer, str) or not customer.strip():
        _fail("license customer must be a non-empty string")

    expires = payload["expires"]
    if not isinstance(expires, str):
        _fail("license expiry must be a YYYY-MM-DD string")
    try:
        expires_date = date.fromisoformat(expires)
    except ValueError:
        _fail("license expiry is not a valid ISO date")
    if expires_date.isoformat() != expires:
        _fail("license expiry must be formatted exactly as YYYY-MM-DD")

    if payload["features"] != ["batch"]:
        _fail("license features must be exactly ['batch']")

    return payload, expires_date


def verify_license(path: Path, *, today: date | None = None) -> dict[str, Any]:
    """Verify a signed license file and return its payload.

    Raises :class:`LicenseError` with a human-readable, PII-free message on
    any failure.  ``today`` defaults to the current UTC date and may be
    injected by tests.
    """
    path = Path(path)
    raw = _read_license(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _fail("license file is not valid UTF-8")
    try:
        envelope = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError:
        _fail("license file is not valid JSON")
    except LicenseError:
        raise
    except RecursionError:
        _fail("license file is too deeply nested")

    if not isinstance(envelope, dict) or set(envelope.keys()) != _ENVELOPE_KEYS:
        _fail("license must be a JSON object with 'payload' and 'signature'")

    signature_b64 = envelope["signature"]
    if not isinstance(signature_b64, str):
        _fail("license signature must be a string")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        _fail("license signature is not valid base64")

    payload, expires_date = _validate_payload(envelope["payload"])

    public_key = _trusted_public_key()
    try:
        public_key.verify(signature, canonical_payload(payload))
    except (InvalidSignature, ValueError):
        _fail("license signature verification failed")

    if today is None:
        today = datetime.now(timezone.utc).date()
    if expires_date < today:
        _fail("license has expired")

    return payload
