"""
Tests for _validate_callback_url SSRF protection.

Validates that the function blocks private, loopback, and link-local IPs
while allowing legitimate public URLs.
"""

import pytest
import sys
from pathlib import Path

# Ensure conftest.py runs first (cwd redirect for server.py import)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server import _validate_callback_url


class TestValidateCallbackUrl:
    """Tests for _validate_callback_url."""

    # ── Should reject ──────────────────────────────────────────────────

    def test_rejects_localhost(self):
        safe, err = _validate_callback_url("http://localhost:8080/callback")
        assert not safe
        assert "localhost" in err

    def test_rejects_127_0_0_1(self):
        safe, err = _validate_callback_url("http://127.0.0.1:5000/callback")
        assert not safe

    def test_rejects_loopback_127_x(self):
        safe, err = _validate_callback_url("http://127.0.0.2/callback")
        assert not safe

    def test_rejects_zero_address(self):
        safe, err = _validate_callback_url("http://0.0.0.0/callback")
        assert not safe

    def test_rejects_private_10_x(self):
        safe, err = _validate_callback_url("http://10.0.0.1/callback")
        assert not safe

    def test_rejects_private_172_16(self):
        safe, err = _validate_callback_url("http://172.16.0.1/callback")
        assert not safe

    def test_rejects_private_192_168(self):
        safe, err = _validate_callback_url("http://192.168.1.1/callback")
        assert not safe

    def test_rejects_link_local(self):
        safe, err = _validate_callback_url("http://169.254.169.254/latest/meta-data/")
        assert not safe

    def test_rejects_file_scheme(self):
        safe, err = _validate_callback_url("file:///etc/passwd")
        assert not safe
        assert "scheme" in err.lower() or "not allowed" in err.lower()

    def test_rejects_ftp_scheme(self):
        safe, err = _validate_callback_url("ftp://example.com/file")
        assert not safe

    def test_rejects_no_hostname(self):
        safe, err = _validate_callback_url("http:///path")
        assert not safe

    def test_rejects_empty_string(self):
        safe, err = _validate_callback_url("")
        assert not safe

    def test_rejects_garbage(self):
        safe, err = _validate_callback_url("not-a-url-at-all")
        assert not safe

    def test_rejects_unresolvable_hostname(self):
        safe, err = _validate_callback_url("https://this-domain-definitely-does-not-exist-xyz123.invalid/cb")
        assert not safe
        assert "resolve" in err.lower()

    # ── Should accept ──────────────────────────────────────────────────

    def test_accepts_public_https(self):
        safe, err = _validate_callback_url("https://example.com/callback")
        assert safe
        assert err is None

    def test_accepts_public_http(self):
        safe, err = _validate_callback_url("http://example.com/callback")
        assert safe
        assert err is None

    def test_accepts_public_with_port(self):
        safe, err = _validate_callback_url("https://example.com:8443/callback")
        assert safe

    def test_accepts_public_with_path(self):
        safe, err = _validate_callback_url("https://example.com/v1/hub/callback")
        assert safe

    # ── IPv6 ───────────────────────────────────────────────────────────

    def test_rejects_ipv6_loopback(self):
        safe, err = _validate_callback_url("http://[::1]/callback")
        assert not safe

    def test_rejects_ipv4_mapped_ipv6_loopback(self):
        safe, err = _validate_callback_url("http://[::ffff:127.0.0.1]/callback")
        assert not safe

    def test_rejects_ipv4_mapped_ipv6_private(self):
        safe, err = _validate_callback_url("http://[::ffff:10.0.0.1]/callback")
        assert not safe

    # ── Carrier-Grade NAT (100.64.0.0/10) ─────────────────────────────

    def test_rejects_carrier_grade_nat(self):
        safe, err = _validate_callback_url("http://100.64.0.1/callback")
        assert not safe

    # ── Edge cases: clearing callback ──────────────────────────────────

    def test_empty_callback_clears(self):
        """Empty string should fail validation — callers should skip
        validation when new_callback is falsy (clearing the URL)."""
        safe, _ = _validate_callback_url("")
        assert not safe
