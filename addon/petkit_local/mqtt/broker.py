"""Embedded MQTT broker using amqtt with Aliyun IoT auth.

The device connects over TLS (Aliyun securemode=2). We keep a plain-TCP
listener for the internal bridge and, when enabled, add a TLS listener on the
device-facing port. The device patches mbedtls to skip cert verification, so a
self-signed cert is sufficient.

This broker exists so the device has somewhere to connect that behaves like
Aliyun IoT; it is NOT Home Assistant's broker (that one is Mosquitto, spoken to
by `ha/publisher.py`). Both run in the same process, and `mqtt/bridge.py` is
the client that joins them.
"""
from __future__ import annotations

import datetime
import logging
import os
import ssl
from typing import TYPE_CHECKING

from amqtt.broker import Broker, BrokerConfig
from amqtt.contexts import ListenerConfig

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.mqtt.auth import AliyunAuthPlugin, parse_client_id

if TYPE_CHECKING:
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)


def _get_host_ip() -> str | None:
    """Best-effort host LAN IP, for the generated cert's SAN.

    Two sources, in order of trust: the Supervisor API (authoritative inside a
    HA OS install) and a UDP socket "connected" to a public address, which
    makes the kernel pick the outbound interface without sending a packet.
    Both are wrapped: a cert with no IP SAN still works for every client that
    isn't verifying the hostname, so failing here must not stop the broker.
    """
    try:
        from petkit_local.config import _supervisor_host_ip
        ip = _supervisor_host_ip()
        if ip:
            return ip
    except Exception:
        pass
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def _cert_covers(cert_path: str, hosts: list[str]) -> bool:
    """Whether the existing cert's SAN already covers every host in `hosts`.

    Load-bearing for the bucket: the device uploads media to `api_url`'s host
    (a LoadBalancer IP, not this pod's), and unlike MQTT — where the patched
    mbedtls skips verification — the `cloud` binary DOES verify the bucket's TLS
    cert against the connect address. A cert whose only IP SAN is the pod IP is
    rejected at the handshake, so the device connects to :9000 and never PUTs.
    Returns False (→ regenerate) when it cannot prove coverage.
    """
    if not hosts:
        return True
    try:
        from cryptography import x509
        import ipaddress
        cert = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        cert_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        cert_dns = set(san.get_values_for_type(x509.DNSName))
        for h in hosts:
            try:
                if str(ipaddress.ip_address(h)) not in cert_ips:
                    return False
            except ValueError:
                if h not in cert_dns:
                    return False
        return True
    except Exception:
        return False


def _cert_is_ca(cert_path: str) -> bool:
    """Whether the existing cert is a usable self-signed trust anchor.

    The `cloud` media uploader's libcurl verifies the bucket cert strictly
    against `/app/bin/ca.crt`. A self-signed leaf with no `basicConstraints`
    (our original certs) cannot anchor its own chain, so verification fails with
    "SSL peer certificate ... was not OK" even though the exact cert is in the
    bundle — while the non-verifying log-upload path still succeeds, which is why
    devlog uploads worked but media never did. Returns False (→ regenerate) for
    a cert missing `basicConstraints: CA:TRUE`, so a stale non-CA cert on the
    PVC is replaced on upgrade rather than silently kept by `_cert_covers`.
    """
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(open(cert_path, "rb").read())
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
        return bool(bc.ca)
    except Exception:
        return False


def ensure_self_signed(cert_path: str, key_path: str, extra_hosts: list[str] | None = None) -> bool:
    """Generate a self-signed cert/key pair at the given paths if missing.

    Self-signed is sufficient because the device's mbedtls is patched to skip
    verification on MQTT (see the repo's CLAUDE.md on cert pinning). The bucket
    is different: the `cloud` binary verifies the cert against the address it
    uploads to, so `extra_hosts` (the bucket/api_url host) MUST be in the SAN or
    the upload fails at the TLS handshake. An existing cert that does not cover
    them is regenerated in place.

    Returns:
        True if a usable cert exists afterwards, including the case where one
        was already there. False means `cryptography` is missing or generation
        failed; the caller then starts without a TLS listener rather than not
        starting at all.
    """
    extra_hosts = [h for h in (extra_hosts or []) if h]
    if os.path.exists(cert_path) and os.path.exists(key_path):
        if _cert_covers(cert_path, extra_hosts) and _cert_is_ca(cert_path):
            return True
        log.info("Cert %s missing SAN host(s) %s or not a CA — regenerating",
                 cert_path, extra_hosts)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        import ipaddress

        host_ip = _get_host_ip()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "petkit-local")])
        san_names = [x509.DNSName("petkit-local")]
        seen_ips: set[str] = set()

        def _add_ip(ip_str: str) -> None:
            # Add each IP as BOTH an IPAddress and a DNSName SAN. The device's
            # TLS stack matches the literal connect address ("10.40.0.33")
            # against DNS-type SANs and does not reliably match IPAddress SANs,
            # so an IP-only SAN fails hostname verification in the cloud
            # uploader. Belt-and-suspenders: cover both SAN types.
            if ip_str in seen_ips:
                return
            san_names.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
            san_names.append(x509.DNSName(ip_str))
            seen_ips.add(ip_str)

        if host_ip:
            try:
                _add_ip(str(ipaddress.ip_address(host_ip)))
            except ValueError:
                pass
        # The bucket/api_url host the device actually connects to.
        for h in extra_hosts:
            try:
                _add_ip(str(ipaddress.ip_address(h)))
            except ValueError:
                san_names.append(x509.DNSName(h))
        now = datetime.datetime.utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san_names), critical=False)
            # A self-signed cert the `cloud` uploader verifies strictly must be a
            # valid trust anchor, or libcurl rejects it ("SSL peer certificate
            # was not OK") even when it is in /app/bin/ca.crt. CA:TRUE + certSign
            # make it anchor its own single-cert chain.
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(
                digital_signature=True, key_encipherment=True, key_cert_sign=True,
                crl_sign=True, content_commitment=False, data_encipherment=False,
                key_agreement=False, encipher_only=False, decipher_only=False),
                critical=True)
            .add_extension(
                x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False)
            .sign(key, hashes.SHA256())
        )
        os.makedirs(os.path.dirname(cert_path) or ".", exist_ok=True)
        with open(key_path, "wb") as f:
            f.write(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ))
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))
        log.info("Generated self-signed MQTT cert at %s", cert_path)
        return True
    except Exception as e:
        log.error("Could not generate self-signed cert: %s", e)
        return False


async def start_broker(
    port: int = 1883,
    registry: DeviceRegistry | None = None,
    *,
    tls: bool = False,
    tls_port: int = 443,
    certfile: str = "",
    keyfile: str = "",
    strict_auth: bool = False,
    hub: EventHub | None = None,
) -> Broker:
    """Start the embedded broker and hand the auth plugin its dependencies.

    amqtt instantiates plugins itself from the config, so the registry, strict
    flag and hub can only be injected afterwards by finding the live instance
    in `broker.plugins_manager` — hence the search below rather than a
    constructor argument.

    Args:
        port: Plain-TCP listener, used by the internal bridge.
        tls_port/certfile/keyfile: Only consulted when `tls` is set; a missing
            cert is generated. If it cannot be, the broker still starts without
            the TLS listener — a plain-HTTP device keeps working.
        strict_auth: Reject devices that fail signature validation. Off by
            default (see AliyunAuthPlugin).

    Returns:
        The started Broker. The caller owns shutting it down.
    """
    listeners = {
        # Plain listener — used by the internal bridge (and plain devices).
        "default": ListenerConfig(type="tcp", bind=f"0.0.0.0:{port}", max_connections=50),
    }

    if tls:
        cert = certfile or "/data/certs/broker.crt"
        key = keyfile or "/data/certs/broker.key"
        if ensure_self_signed(cert, key):
            listeners["tls"] = ListenerConfig(
                type="tcp", bind=f"0.0.0.0:{tls_port}", max_connections=50,
                ssl=True, certfile=cert, keyfile=key,
            )
            log.info("MQTT TLS listener enabled on port %d", tls_port)
        else:
            log.warning("TLS requested but no cert available - TLS listener not started")

    config = BrokerConfig(
        listeners=listeners,
        timeout_disconnect_delay=2,
        plugins={"petkit_local.mqtt.auth.AliyunAuthPlugin": {}},
    )

    broker = Broker(config)

    # The device's mbedtls offers only RSA key-exchange and PSK suites — no
    # ECDHE, no DHE. Python 3.12's ssl.create_default_context() excludes plain
    # RSA (no forward secrecy), so the TLS handshake fails with no shared
    # cipher. We override _create_ssl_context to accept what the device sends.
    _orig = Broker._create_ssl_context

    @staticmethod
    def _device_friendly_ssl_context(listener):
        ctx = _orig(listener)
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    broker._create_ssl_context = _device_friendly_ssl_context

    await broker.start()

    if registry:
        for plugin_wrapper in broker.plugins_manager._plugins:
            obj = plugin_wrapper.plugin_object if hasattr(plugin_wrapper, 'plugin_object') else plugin_wrapper
            if isinstance(obj, AliyunAuthPlugin):
                obj.set_registry(registry)
                obj.set_strict(strict_auth)
                obj.set_hub(hub)
                obj.set_broker(broker)
                log.info("AliyunAuthPlugin registered (strict=%s)", strict_auth)
                break
        else:
            # Not fatal, but worth shouting about: without the plugin the
            # broker falls back to amqtt's own auth and no device connection
            # will ever be attributed to a registry entry.
            log.warning("AliyunAuthPlugin not found in broker plugins - "
                        "device MQTT sessions will not be recognised")

    log.info("MQTT broker started on port %d %s", port, "(+TLS)" if tls else "")
    return broker


def delivery_view(broker, product_key: str, device_name: str) -> dict:
    """What the BROKER will actually deliver to one device, right now.

    Everything else in this add-on reports intent: the auth plugin records the
    filters it asked for, and a publish reports that it was accepted. Neither
    is delivery. MQTT has no delivery report, so when a command changes nothing
    on the box the question "would a publish to that topic have reached it at
    all" had no answer anywhere — and it is the one question that separates a
    broker problem from a firmware one.

    Read straight off `Broker._subscriptions`, which is what `_run_broadcast`
    iterates, so this reports the same table delivery consults rather than a
    parallel copy that could drift from it. Sessions are matched by client id
    rather than by identity, so no session handle has to be plumbed here.

    Returns:
        `{"filters": [...], "sessions": [{"client_id", "state"}]}`. A session
        whose state is not "connected" is skipped by the broadcast loop, which
        is why the state is reported and not just the count.
    """
    subscriptions = getattr(broker, "_subscriptions", None) or {}

    def owns(session) -> bool:
        parsed = parse_client_id(getattr(session, "client_id", "") or "")
        return bool(parsed and parsed["product_key"] == product_key
                    and parsed["device_name"] == device_name)

    filters, sessions = [], {}
    for topic_filter, entries in subscriptions.items():
        for session, _qos in entries or ():
            if not owns(session):
                continue
            if topic_filter not in filters:
                filters.append(topic_filter)
            cid = getattr(session, "client_id", "")
            sessions[cid] = getattr(getattr(session, "transitions", None), "state", "?")
    return {
        "filters": sorted(filters),
        "sessions": [{"client_id": c, "state": s} for c, s in sessions.items()],
    }
