"""Embedding the server certificate in generated packages (spec section 24).

An endpoint cannot reach the management server until it trusts the server's
certificate, and the agent deliberately does no custom TLS validation. Where
there is no GPO to push the certificate, the installer carries it -- so these
tests cover the staging step that feeds WiX.
"""

from __future__ import annotations

import base64
import datetime
import hashlib

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app import create_app
from app.config import TestConfig
from app.services.packaging import _stage_server_certificate


def _self_signed() -> tuple[bytes, bytes]:
    """Return (pem, der) for a throwaway self-signed certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "eem-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        # CA:FALSE is what makes trusting this leaf a narrow grant: as an anchor
        # it vouches for itself and cannot sign anything else.
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        cert.public_bytes(serialization.Encoding.DER),
    )


@pytest.fixture(scope="module")
def certificate() -> tuple[bytes, bytes]:
    return _self_signed()


def _stage(tmp_path, cert_path: str):
    app = create_app(TestConfig())
    app.config["PACKAGE_CA_CERT_PATH"] = cert_path
    with app.app_context():
        return _stage_server_certificate(tmp_path)


def test_pem_is_converted_to_der_with_matching_thumbprint(tmp_path, certificate):
    pem, der = certificate
    source = tmp_path / "cert.pem"
    source.write_bytes(pem)

    result = _stage(tmp_path, str(source))
    assert result is not None
    staged, thumbprint = result

    # WiX embeds this file; Windows needs DER, not PEM.
    assert staged.read_bytes() == der
    # certutil -delstore matches on the SHA-1 thumbprint, so it must be the
    # thumbprint of the DER encoding -- the same value certutil will compute.
    assert thumbprint == hashlib.sha1(der).hexdigest()
    assert x509.load_der_x509_certificate(staged.read_bytes())


def test_der_input_is_passed_through(tmp_path, certificate):
    _, der = certificate
    source = tmp_path / "cert.cer"
    source.write_bytes(der)

    result = _stage(tmp_path, str(source))
    assert result is not None
    staged, thumbprint = result
    assert staged.read_bytes() == der
    assert thumbprint == hashlib.sha1(der).hexdigest()


def test_only_the_leaf_is_taken_from_a_chain(tmp_path, certificate):
    """A PFX-derived PEM holds the leaf followed by its issuers.

    Embedding an issuing CA would be a far broader grant than embedding the
    leaf, so the first block -- the leaf -- is the one that must be used.
    """
    leaf_pem, leaf_der = certificate
    issuer_pem, issuer_der = _self_signed()
    source = tmp_path / "fullchain.pem"
    source.write_bytes(leaf_pem + issuer_pem)

    result = _stage(tmp_path, str(source))
    assert result is not None
    staged, thumbprint = result
    assert staged.read_bytes() == leaf_der
    assert staged.read_bytes() != issuer_der
    assert thumbprint == hashlib.sha1(leaf_der).hexdigest()


def test_no_certificate_configured_stages_nothing(tmp_path):
    assert _stage(tmp_path, "") is None


def test_missing_file_stages_nothing_rather_than_failing(tmp_path):
    """A misconfigured path must not break package generation entirely."""
    assert _stage(tmp_path, str(tmp_path / "does-not-exist.pem")) is None


def test_garbage_pem_stages_nothing(tmp_path):
    source = tmp_path / "cert.pem"
    source.write_bytes(
        b"-----BEGIN CERTIFICATE-----\n!!! not base64 !!!\n-----END CERTIFICATE-----\n"
    )
    assert _stage(tmp_path, str(source)) is None


def test_empty_pem_body_stages_nothing(tmp_path):
    source = tmp_path / "cert.pem"
    source.write_bytes(b"-----BEGIN CERTIFICATE-----\n\n-----END CERTIFICATE-----\n")
    assert _stage(tmp_path, str(source)) is None


def test_thumbprint_is_lowercase_hex_certutil_accepts(tmp_path, certificate):
    """certutil matches thumbprints case-insensitively, but the value must be
    bare hex with no separators."""
    pem, _ = certificate
    source = tmp_path / "cert.pem"
    source.write_bytes(pem)

    _, thumbprint = _stage(tmp_path, str(source))
    assert len(thumbprint) == 40
    assert all(c in "0123456789abcdef" for c in thumbprint)


def test_toolchain_status_reports_whether_a_certificate_will_be_embedded(tmp_path, certificate):
    from app.services.packaging import toolchain_status

    pem, _ = certificate
    source = tmp_path / "cert.pem"
    source.write_bytes(pem)

    app = create_app(TestConfig())
    app.config["PACKAGE_CA_CERT_PATH"] = str(source)
    with app.app_context():
        assert toolchain_status()["certificateEmbedded"] is True

    app.config["PACKAGE_CA_CERT_PATH"] = ""
    with app.app_context():
        assert toolchain_status()["certificateEmbedded"] is False


def test_base64_with_whitespace_is_decoded(tmp_path, certificate):
    """PEM bodies are line-wrapped; the decoder must tolerate the newlines."""
    _, der = certificate
    wrapped = base64.encodebytes(der)  # inserts newlines every 76 chars
    source = tmp_path / "cert.pem"
    source.write_bytes(
        b"-----BEGIN CERTIFICATE-----\n" + wrapped + b"-----END CERTIFICATE-----\n"
    )

    result = _stage(tmp_path, str(source))
    assert result is not None
    assert result[0].read_bytes() == der
