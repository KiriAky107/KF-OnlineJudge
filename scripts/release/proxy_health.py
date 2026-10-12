"""Read-only fixed HTTPS reachability, pinned certificate and hostname check."""

import hashlib
import http.client
import socket
import ssl
from urllib.parse import urlsplit

from . import proxy_service
from .policy import DeliveryError, read_json


def observe(selected: dict) -> dict:
    directory = proxy_service.batch(selected)
    spec = read_json(directory / "launch.json", 4096)
    host = urlsplit(spec["origin"]).hostname
    # This explicit Root-protected leaf/chain is already byte-bound to selected
    # TLS material; it is the local health trust anchor, not a claim that public
    # browsers or the system CA store trust it.
    certificate = proxy_service.ROOT / "tls/fullchain.pem"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=certificate)
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    with socket.create_connection(("127.0.0.1", 443), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as client:
            client.settimeout(5)
            der = client.getpeercert(binary_form=True)
            pem = certificate.read_text()
            leaf = pem[: pem.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")]
            if (
                hashlib.sha256(der).digest()
                != hashlib.sha256(ssl.PEM_cert_to_DER_cert(leaf)).digest()
            ):
                raise DeliveryError("HTTPS served another certificate")
            client.sendall(
                (
                    f"GET /api/v1/health/live HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n"
                ).encode()
            )
            response = http.client.HTTPResponse(client)
            try:
                response.begin()
                body = response.read(4097)
                if response.status != 200 or len(body) > 4096:
                    raise DeliveryError("HTTPS health response exceeds bound or is unavailable")
                from .operator_units import decode

                value = decode(body, 4096)
                if (
                    not isinstance(value, dict)
                    or value.get("status") != "ok"
                    or value.get("service") != "platform"
                ):
                    raise DeliveryError("HTTPS/API health response unavailable")
            except http.client.HTTPException as error:
                raise DeliveryError("HTTPS health framing unavailable") from error
            finally:
                response.close()
    return {
        "reachable": True,
        "pinned_certificate_and_hostname_verified": True,
        "public_browser_trust_verified": False,
    }
