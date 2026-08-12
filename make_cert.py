r"""
One-time (or re-run when the machine's LAN IP changes) self-signed TLS cert
generator for the team-facing MCP server.

Creates certs/warehouse-mcp.crt + certs/warehouse-mcp.key with SANs for this
machine's hostname, localhost, and every current LAN IPv4 — Chromium-based
clients (Claude Desktop) validate against SANs, not CN, so the names teammates
type into the connector URL must appear here. server.py --http picks the pair
up automatically and serves https.

Teammates install the .crt (NOT the .key — that stays on this machine) into
their Trusted Root store once, or point NODE_EXTRA_CA_CERTS at it if they
connect via mcp-remote; see SHARING.md.

    .venv\Scripts\python.exe make_cert.py            # auto-detect names
    .venv\Scripts\python.exe make_cert.py extra.name 10.1.2.3   # add SANs

Requires the `cryptography` package (pip install cryptography) — not in
requirements.txt by default since most setups only need stdio, not the
team-shared HTTP mode.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs")
CRT_PATH = os.path.join(CERT_DIR, "warehouse-mcp.crt")
KEY_PATH = os.path.join(CERT_DIR, "warehouse-mcp.key")
VALID_DAYS = 3650  # local trust anchors are exempt from Chromium's 398-day cap
# Shown as the certificate's Organization field. Cosmetic only — clients trust
# the cert by SAN + Trusted Root install, not by this string. Override with
# CERT_ORG_NAME in your environment if you want your own team/company name here.
ORG_NAME = os.environ.get("CERT_ORG_NAME", "ecommerce-warehouse MCP")


def lan_ipv4s() -> list[str]:
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                ips.add(ip)
    except socket.gaierror:
        pass
    # the "connect a UDP socket" trick finds the default-route interface even
    # when the hostname doesn't resolve to it
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def fqdn_for(hostname: str) -> str | None:
    """This machine's domain name, e.g. mybox.corp.example.com.

    Accepted only when its first label IS our hostname: a hostile or wildcard
    resolver can answer getfqdn() with anything, and server.py's host policy
    applies the identical guard, so the cert and the allowlist stay in step.
    """
    try:
        fqdn = socket.getfqdn().rstrip(".")
    except OSError:
        return None
    if not fqdn or "." not in fqdn:
        return None
    if fqdn.lower().split(".")[0] != hostname.lower():
        print(f"NOTE: ignoring getfqdn() answer {fqdn!r} — first label is not "
              f"{hostname!r}; pass it explicitly if it is really ours")
        return None
    return fqdn


def main() -> None:
    hostname = socket.gethostname()
    # server.py's HostGuard accepts exactly these name shapes, so the SANs must
    # carry them or clients fail at the TLS layer before the Host check runs.
    dns_names = {hostname, hostname.lower(), "localhost", f"{hostname.lower()}.local"}
    fqdn = fqdn_for(hostname)
    if fqdn:
        dns_names.update({fqdn, fqdn.lower()})
    ip_names = set(lan_ipv4s()) | {"127.0.0.1"}
    for extra in sys.argv[1:]:
        try:
            ipaddress.ip_address(extra)
            ip_names.add(extra)
        except ValueError:
            dns_names.add(extra)

    san = x509.SubjectAlternativeName(
        [x509.DNSName(n) for n in sorted(dns_names)]
        + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ip_names)]
    )

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG_NAME),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # self-signed
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(san, critical=False)
        # CA:TRUE + keyCertSign lets the cert anchor itself when installed in
        # the Trusted Root store (mkcert-style); serverAuth EKU is what
        # Chromium checks for TLS servers
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, key_cert_sign=True,
                content_commitment=False, data_encipherment=False,
                key_agreement=False, crl_sign=False,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    os.makedirs(CERT_DIR, exist_ok=True)
    with open(KEY_PATH, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    with open(CRT_PATH, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"Wrote {CRT_PATH}")
    print(f"Wrote {KEY_PATH}  (private — never share this one)")
    print(f"SANs: {', '.join(sorted(dns_names) + sorted(ip_names))}")
    print("Restart the MCP server to serve https, then send teammates the .crt "
          "per SHARING.md.")


if __name__ == "__main__":
    main()
