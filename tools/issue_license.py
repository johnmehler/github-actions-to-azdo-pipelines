#!/usr/bin/env python3
"""Seller-side license tool for gh-to-azdo Pro batch licenses.

This script is NOT part of the distributed package.  It is used by the vendor
to generate an Ed25519 keypair and to sign offline annual licenses that
customers activate with ``gh-to-azdo batch --license``.

Requires the package to be installed first (``pip install -e .``) because it
reuses ``github_to_azdo.licensing.canonical_payload`` for signing.

Private keys are stored as the raw 32-byte Ed25519 seed (or unencrypted PKCS8
DER when loading).  Files are created exclusively (no overwrite) with mode
0600 on POSIX; on Windows the mode is not enforced, so protect the file with
NTFS ACLs and never commit or share it.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import date
from pathlib import Path


def _fail(message: str) -> int:
    print(f"issue-license: error: {message}", file=sys.stderr)
    return 1


def _write_exclusive(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise
    except OSError as exc:
        raise RuntimeError(f"cannot create {path}: {exc.strerror or exc}") from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise RuntimeError(f"cannot write {path}: {exc.strerror or exc}") from exc


def _load_private_key(path: Path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_der_private_key

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"cannot read private key {path}: {exc.strerror or exc}") from exc
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw)
    try:
        key = load_der_private_key(raw, password=None)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("private key is not a raw Ed25519 seed or PKCS8 DER") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("private key is not Ed25519")
    return key


def _cmd_keygen(args: argparse.Namespace) -> int:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )
    except ImportError:
        return _fail("cryptography is not installed; install the package dependencies first")

    private_key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    private_raw = private_key.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption()
    )

    out_path = Path(args.private_key)
    try:
        _write_exclusive(out_path, private_raw)
    except FileExistsError:
        return _fail(f"refusing to overwrite existing file: {out_path}")
    except RuntimeError as exc:
        return _fail(str(exc))

    # stdout carries ONLY the raw public key (standard base64) for provisioning.
    print(public_b64)
    return 0


def _cmd_issue(args: argparse.Namespace) -> int:
    try:
        from github_to_azdo.licensing import canonical_payload
    except ImportError:
        return _fail(
            "github_to_azdo is not importable; run 'pip install -e .' first"
        )

    customer = args.customer.strip()
    if not customer:
        return _fail("customer name must be non-empty")

    expires = args.expires
    try:
        expires_date = date.fromisoformat(expires)
    except ValueError:
        return _fail("expires must be a valid ISO date (YYYY-MM-DD)")
    if expires_date.isoformat() != expires:
        return _fail("expires must be formatted exactly as YYYY-MM-DD")

    try:
        private_key = _load_private_key(Path(args.private_key))
    except RuntimeError as exc:
        return _fail(str(exc))

    payload = {
        "version": 1,
        "customer": customer,
        "expires": expires,
        "features": ["batch"],
    }
    try:
        signature = private_key.sign(canonical_payload(payload))
    except Exception as exc:  # noqa: BLE001
        return _fail(f"signing failed: {exc}")

    envelope = {
        "payload": payload,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    out_path = Path(args.output)
    try:
        _write_exclusive(out_path, (json.dumps(envelope, indent=2) + "\n").encode("utf-8"))
    except FileExistsError:
        return _fail(f"refusing to overwrite existing file: {out_path}")
    except RuntimeError as exc:
        return _fail(str(exc))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue-license",
        description="Vendor tool: generate Ed25519 keys and sign offline batch licenses.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="generate an Ed25519 keypair")
    keygen.add_argument("--private-key", required=True, help="output path for the private key")

    issue = sub.add_parser("issue", help="sign a license file")
    issue.add_argument("--private-key", required=True, help="Ed25519 private key file")
    issue.add_argument("--customer", required=True, help="customer name")
    issue.add_argument("--expires", required=True, help="expiry date YYYY-MM-DD")
    issue.add_argument("--output", required=True, help="output license JSON path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "keygen":
        return _cmd_keygen(args)
    if args.command == "issue":
        return _cmd_issue(args)
    return _fail(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
