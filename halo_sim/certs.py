"""Local HTTPS certificates so phones can use camera/mic (secure context).

Creates a tiny local CA plus a server certificate for this Mac's LAN
address(es) under ``~/.halo-sim/certs``. Install ``ca.crt`` on the phone once
(served at ``http://<mac>:8080/ca.crt``) and the HTTPS page is fully trusted,
which iOS Safari needs for secure WebSockets.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
from pathlib import Path

CERT_DIR = Path.home() / ".halo-sim" / "certs"


def lan_ips() -> list[str]:
    """LAN IPv4 addresses, the default-route interface (usually Wi-Fi) first."""
    ips: list[str] = []
    try:
        out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=5).stdout
        iface = next((l.split(":", 1)[1].strip() for l in out.splitlines() if "interface:" in l), None)
        if iface:
            ip = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True,
                                timeout=5).stdout.strip()
            if ip:
                ips.append(ip)
    except Exception:
        pass
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "-->" not in line:  # skip point-to-point links (VPN, USB)
                ip = line.split()[1]
                if not ip.startswith("127.") and ip not in ips:
                    ips.append(ip)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    return ips


def mdns_name() -> str | None:
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
        return f"{name}.local" if name else None
    except Exception:
        return None


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True, cwd=CERT_DIR)


def ensure_certs() -> tuple[Path, Path, Path]:
    """Return (server_crt, server_key, ca_crt), (re)creating them when the LAN names change."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key = CERT_DIR / "ca.crt", CERT_DIR / "ca.key"
    crt, key, meta = CERT_DIR / "server.crt", CERT_DIR / "server.key", CERT_DIR / "names.json"
    names = {"ips": sorted(set(lan_ips()) | {"127.0.0.1"}), "dns": sorted({"localhost", mdns_name() or "localhost"})}

    if not (ca_crt.exists() and ca_key.exists()):
        _run("openssl", "req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-sha256",
             "-keyout", "ca.key", "-out", "ca.crt", "-days", "825",
             "-subj", "/CN=Halo SIM Local CA/O=halo-sim",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        crt.unlink(missing_ok=True)

    old = json.loads(meta.read_text()) if meta.exists() else None
    if not crt.exists() or old != names:
        san = ",".join([f"DNS:{d}" for d in names["dns"]] +
                       [f"IP:{ipaddress.ip_address(i)}" for i in names["ips"]])
        (CERT_DIR / "ext.cnf").write_text(
            f"subjectAltName={san}\nextendedKeyUsage=serverAuth\n"
            "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\n")
        _run("openssl", "req", "-new", "-nodes", "-newkey", "rsa:2048", "-sha256",
             "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=halo-sim")
        _run("openssl", "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
             "-CAcreateserial", "-out", "server.crt", "-days", "825", "-sha256",
             "-extfile", "ext.cnf")
        meta.write_text(json.dumps(names))
    return crt, key, ca_crt
