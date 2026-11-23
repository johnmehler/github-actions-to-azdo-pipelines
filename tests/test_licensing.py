"""Tests for github_to_azdo.licensing."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from github_to_azdo import licensing
from github_to_azdo.licensing import LicenseError, canonical_payload, verify_license


def make_keypair():
    private = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    return private, public_b64


def payload_dict(customer="Acme Corp", expires="2099-01-01"):
    return {
        "version": 1,
        "customer": customer,
        "expires": expires,
        "features": ["batch"],
    }


def sign_envelope(private, payload) -> dict:
    return {
        "payload": payload,
        "signature": base64.b64encode(private.sign(canonical_payload(payload))).decode("ascii"),
    }


def write_license(directory: Path, envelope_or_text, name="license.json") -> Path:
    path = directory / name
    if isinstance(envelope_or_text, str):
        path.write_text(envelope_or_text, encoding="utf-8")
    else:
        path.write_text(json.dumps(envelope_or_text), encoding="utf-8")
    return path


class TestCanonicalPayload(unittest.TestCase):
    def test_exact_bytes(self):
        payload = {"b": 2, "a": 1}
        self.assertEqual(canonical_payload(payload), b'{"a":1,"b":2}')

    def test_ascii_escaped(self):
        payload = {"customer": "Renée"}
        self.assertEqual(
            canonical_payload(payload), '{"customer":"Ren\\u00e9e"}'.encode("ascii")
        )


class TestVerifyLicense(unittest.TestCase):
    def setUp(self):
        self.private, self.public_b64 = make_keypair()
        patcher = mock.patch.object(licensing, "PRO_PUBLIC_KEY_B64", self.public_b64)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _license(self, payload=None, private=None) -> Path:
        payload = payload if payload is not None else payload_dict()
        return write_license(self.dir, sign_envelope(private or self.private, payload))

    def test_valid_license(self):
        path = self._license()
        result = verify_license(path, today=date(2025, 6, 1))
        self.assertEqual(result["customer"], "Acme Corp")
        self.assertEqual(result["expires"], "2099-01-01")
        self.assertEqual(result["features"], ["batch"])
        self.assertEqual(result["version"], 1)

    def test_expiry_is_inclusive(self):
        path = self._license(payload_dict(expires="2025-06-01"))
        result = verify_license(path, today=date(2025, 6, 1))
        self.assertEqual(result["customer"], "Acme Corp")

    def test_expired_license(self):
        path = self._license(payload_dict(expires="2025-01-01"))
        with self.assertRaises(LicenseError) as ctx:
            verify_license(path, today=date(2025, 6, 1))
        self.assertIn("expired", str(ctx.exception))

    def test_tampered_payload_fails(self):
        path = self._license()
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["payload"]["customer"] = "Someone Else"
        tampered = write_license(self.dir, envelope, name="tampered.json")
        with self.assertRaises(LicenseError):
            verify_license(tampered, today=date(2025, 6, 1))

    def test_tampered_signature_fails(self):
        path = self._license()
        envelope = json.loads(path.read_text(encoding="utf-8"))
        sig = bytearray(base64.b64decode(envelope["signature"]))
        sig[0] ^= 0xFF
        envelope["signature"] = base64.b64encode(bytes(sig)).decode("ascii")
        tampered = write_license(self.dir, envelope, name="badsig.json")
        with self.assertRaises(LicenseError):
            verify_license(tampered, today=date(2025, 6, 1))

    def test_wrong_key_fails(self):
        other_private, _ = make_keypair()
        path = self._license(private=other_private)
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_unset_public_key_fails_closed(self):
        path = self._license()
        with mock.patch.object(licensing, "PRO_PUBLIC_KEY_B64", ""):
            with self.assertRaises(LicenseError) as ctx:
                verify_license(path, today=date(2025, 6, 1))
        self.assertIn("public key", str(ctx.exception))

    def test_malformed_public_key_fails_closed(self):
        path = self._license()
        with mock.patch.object(licensing, "PRO_PUBLIC_KEY_B64", "!!!not-base64!!!"):
            with self.assertRaises(LicenseError):
                verify_license(path, today=date(2025, 6, 1))

    def test_not_json(self):
        path = write_license(self.dir, "this is not json", name="bad.json")
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_duplicate_keys_rejected(self):
        text = (
            '{"payload": {"version": 1, "version": 1, "customer": "x", "expires": "2099-01-01",'
            ' "features": ["batch"]}, "signature": "AAAA"}'
        )
        path = write_license(self.dir, text, name="dup.json")
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_missing_envelope_key(self):
        path = write_license(self.dir, {"payload": payload_dict()}, name="missing.json")
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_extra_envelope_key(self):
        envelope = sign_envelope(self.private, payload_dict())
        envelope["extra"] = True
        path = write_license(self.dir, envelope, name="extra.json")
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_version_must_be_int_one(self):
        for version in (2, "1", True, 1.0):
            with self.subTest(version=version):
                payload = payload_dict()
                payload["version"] = version
                envelope = sign_envelope(self.private, payload)
                path = write_license(self.dir, envelope, name=f"v{version}.json")
                with self.assertRaises(LicenseError):
                    verify_license(path, today=date(2025, 6, 1))

    def test_customer_must_be_nonblank(self):
        for customer in ("", "   ", None, 42):
            with self.subTest(customer=customer):
                payload = payload_dict()
                payload["customer"] = customer
                envelope = sign_envelope(self.private, payload)
                path = write_license(self.dir, envelope, name="cust.json")
                with self.assertRaises(LicenseError):
                    verify_license(path, today=date(2025, 6, 1))

    def test_expires_must_be_exact_iso(self):
        for expires in ("2099-1-1", "2099/01/01", "20990101", 20990101, None):
            with self.subTest(expires=expires):
                payload = payload_dict()
                payload["expires"] = expires
                envelope = sign_envelope(self.private, payload)
                path = write_license(self.dir, envelope, name="exp.json")
                with self.assertRaises(LicenseError):
                    verify_license(path, today=date(2025, 6, 1))

    def test_features_must_be_batch(self):
        for features in ([], ["batch", "extra"], "batch", ["pro"]):
            with self.subTest(features=features):
                payload = payload_dict()
                payload["features"] = features
                envelope = sign_envelope(self.private, payload)
                path = write_license(self.dir, envelope, name="feat.json")
                with self.assertRaises(LicenseError):
                    verify_license(path, today=date(2025, 6, 1))

    def test_signature_must_be_valid_base64(self):
        envelope = sign_envelope(self.private, payload_dict())
        envelope["signature"] = "!!!not-base64!!!"
        path = write_license(self.dir, envelope, name="sig64.json")
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_oversized_license_rejected(self):
        path = self.dir / "big.json"
        path.write_bytes(b" " * (17 * 1024))
        with self.assertRaises(LicenseError):
            verify_license(path, today=date(2025, 6, 1))

    def test_missing_file(self):
        with self.assertRaises(LicenseError):
            verify_license(self.dir / "nope.json", today=date(2025, 6, 1))

    def test_no_pii_in_errors(self):
        path = self._license(payload_dict(customer="Sensitive Customer Name"))
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["signature"] = "AAAA"
        bad = write_license(self.dir, envelope, name="pii.json")
        with self.assertRaises(LicenseError) as ctx:
            verify_license(bad, today=date(2025, 6, 1))
        self.assertNotIn("Sensitive", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
