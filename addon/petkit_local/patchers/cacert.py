"""CA certificate patcher.

Appends the add-on's self-signed TLS certificate to the device's Mozilla CA
bundle (/app/bin/ca.crt) so the cloud binary trusts our local bucket for media
uploads.

All patching is done server-side: download ca.crt → append our cert PEM → upload
back → bind-mount + restart cloud.
"""
from __future__ import annotations

import logging
import os

from petkit_local.patchers.common import md5hex
from petkit_local.patchers.verify import assert_ca_bundle

log = logging.getLogger(__name__)


def patch_ca_bundle(original: bytes | None, our_cert_pem: bytes) -> bytes:
    """Put exactly ONE copy of our current self-signed cert in the device's CA
    bundle, dropping any prior copies first.

    Raises:
        ValueError: If either side is not a PEM bundle.

    Both inputs are validated because the failure mode of not doing so is
    severe: this used to treat an empty `original` as "the device has no CA
    bundle" and return a file containing ONLY our certificate, which — written
    over /app/bin/ca.crt — would leave the device unable to verify any other
    TLS peer. Every device ships a bundle (226 KB on a T5, 11 KB on a D4SH), so
    an empty read means the download failed, not that there is nothing to keep.

    Why strip-then-append rather than a blind append: each cert regeneration (a
    SAN or CA-flag fix) mints a new cert, and appending every time left the OLD
    ones in the bundle. The device's OpenSSL 1.0.0 picks the FIRST cert whose
    subject matches the served cert as the trust anchor; if that is a stale,
    different-key copy, verification fails ("SSL peer certificate ... was not
    OK") even though the right cert is also present — which is exactly what
    silently broke media uploads. So every cert sharing our current cert's
    subject is removed and our current cert appended, leaving one. This also
    makes re-applying the patch idempotent instead of an error.
    """
    if b"-----BEGIN CERTIFICATE-----" not in our_cert_pem:
        raise ValueError("our_cert_pem is not valid PEM")

    assert_ca_bundle(original or b"", "device ca.crt")
    assert original is not None  # narrowed by assert_ca_bundle

    import re
    from cryptography import x509

    our = x509.load_pem_x509_certificate(our_cert_pem)
    blocks = re.findall(
        rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        original, re.DOTALL)
    original_count = len(blocks)

    kept: list[bytes] = []
    dropped = 0
    for block in blocks:
        try:
            cert = x509.load_pem_x509_certificate(block)
        except Exception:
            # Keep anything we cannot parse rather than risk losing a real CA.
            kept.append(block.strip())
            continue
        if cert.subject == our.subject:
            dropped += 1
            continue
        kept.append(block.strip())
    kept.append(our_cert_pem.strip())

    patched = b"\n\n".join(kept) + b"\n"
    patched_count = patched.count(b"-----BEGIN CERTIFICATE-----")

    if patched_count < 1 or our_cert_pem.strip() not in patched:
        raise RuntimeError("CA bundle patch produced an unusable result")

    log.info("Patched CA bundle: %d -> %d certs (dropped %d stale copies of "
             "ours), %d -> %d bytes (md5 %s -> %s)",
             original_count, patched_count, dropped, len(original), len(patched),
             md5hex(original), md5hex(patched))
    return patched


def load_our_cert(data_dir: str = "/data") -> bytes:
    """Load the add-on's self-signed cert (generated for the MQTT TLS listener)."""
    cert_path = os.path.join(data_dir, "certs", "broker.crt")
    if not os.path.exists(cert_path):
        raise FileNotFoundError(f"Add-on cert not found at {cert_path} - "
                                "start the add-on once to auto-generate it")
    with open(cert_path, "rb") as f:
        return f.read()


PATCHER_INFO = {
    "id": "cacert",
    "name": "CA Certificate (Bucket TLS)",
    "description": (
        "Required for local storage - the cloud binary verifies the bucket "
        "server's TLS certificate against /app/bin/ca.crt (Mozilla CA bundle). "
        "This patch appends the add-on's self-signed certificate to that bundle "
        "so uploads to our local bucket succeed.\n\n"
        "Apply this together with the Local Storage patch.\n\n"
        "What it does: copies /app/bin/ca.crt to /system/ca_patched.crt, appends "
        "the add-on's certificate, then updates /system/app_init.sh to "
        "bind-mount the patched bundle before the stock init starts cloud."
    ),
    "files": ["/system/ca_patched.crt"],
    # No architecture: this appends PEM text to a certificate bundle and
    # bind-mounts the result. Nothing here is machine code.
    "arch": None,
    # Conservative UI figure: what to tell the user BEFORE we know the model.
    # ca.crt is 226,168 B on a T5 and 11,171 B on a D4SH, plus our PEM.
    "needs_bytes": 524288,
}
