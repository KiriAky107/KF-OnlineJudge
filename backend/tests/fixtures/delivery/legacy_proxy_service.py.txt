"""Fixed ordinary HTTPS service profile, separate from TLS/config installation."""

import re
from pathlib import Path

from . import install, proxy
from .policy import DeliveryError, fields, sha256
from .services import python_environment

UID = 1205
USER = "kf-proxy"
UNIT = "kf-proxy.service"
ROOT = Path("/etc/kf-proxy")
UNITS = ROOT / "units"
DATA = Path("/var/lib/kf-onlinejudge/proxy")
CAPACITY = 128 * 1024**2
CAPABILITY = 1 << 10  # Linux CAP_NET_BIND_SERVICE, solely for the fixed TLS listener.


def validate_receipt(value: dict) -> dict:
    fields(
        value,
        {
            "schema_version",
            "source_sha",
            "manifest_sha256",
            "origin",
            "tls",
            "configuration_sha256",
            "installed",
            "tls_verified",
            "services_started",
            "execution_verified",
        },
        "proxy source receipt",
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["source_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", value["source_sha"])
        or any(
            value[k] is not False
            for k in ("installed", "tls_verified", "services_started", "execution_verified")
        )
    ):
        raise DeliveryError("proxy source receipt authority differs")
    fields(value["tls"], {"certificate_sha256", "key_sha256"}, "proxy TLS fingerprints")
    proxy.validate_profile({"schema_version": 1, "origin": value["origin"], **value["tls"]})
    sha256(value["manifest_sha256"])
    sha256(value["configuration_sha256"])
    return value


def batch(selected: dict) -> Path:
    if not re.fullmatch(r"sd-[0-9a-f]{12}", selected["release_id"]) or not re.fullmatch(
        r"cfg-[0-9a-f]{24}", selected["configuration_id"]
    ):
        raise DeliveryError("invalid proxy service binding")
    return ROOT / (selected["release_id"] + "." + selected["configuration_id"])


def launch_spec(selected: dict, receipt: dict) -> dict:
    validate_receipt(receipt)
    if (
        receipt["source_sha"][:12] != selected["release_id"][3:]
        or receipt["manifest_sha256"] != selected["manifest_sha256"]
    ):
        raise DeliveryError("proxy is not this selected release")
    return {
        "schema_version": 1,
        "release_id": selected["release_id"],
        "configuration_id": selected["configuration_id"],
        "origin": receipt["origin"],
        "configuration_sha256": receipt["configuration_sha256"],
        **receipt["tls"],
    }


def render(selected: dict) -> bytes:
    batch(selected)
    payload = install.RELEASES / selected["release_id"] / "payload"
    arguments = (
        f" --release-id {selected['release_id']} --configuration-id {selected['configuration_id']}"
    )
    clean = "/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C.UTF-8 " + python_environment(payload)
    python = str(payload / "python/bin/python3.13")
    lines = [
        "[Unit]",
        "Description=KF bounded ordinary HTTPS entry",
        "After=local-fs.target kf-api.service",
        "StartLimitIntervalSec=600",
        "StartLimitBurst=3",
        "[Service]",
        "Type=exec",
        "Slice=kfonlinejudge.slice",
        "User=kf-proxy",
        "Group=kf-proxy",
        "UMask=0077",
        "WorkingDirectory=" + str(payload),
        "RuntimeDirectory=kf-proxy",
        "RuntimeDirectoryMode=0700",
        "ExecStartPre=+"
        + clean
        + " "
        + python
        + " -s -P -m scripts.release.proxy_install --check-binding"
        + arguments,
        "ExecStart=" + clean + " " + python + " -s -P -m scripts.release.proxy_guard" + arguments,
        "MemoryMax=128M",
        "MemorySwapMax=0",
        "CPUQuota=50%",
        "TasksMax=16",
        "LimitCORE=0",
        "LimitFSIZE=8M",
        "LimitNOFILE=256",
        "LimitSTACK=16M",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
        "AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "ProtectControlGroups=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "PrivatePIDs=yes",
        "ProtectProc=invisible",
        "RestrictSUIDSGID=yes",
        "LockPersonality=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET",
        "ReadWritePaths=" + str(DATA),
        "RequiresMountsFor=" + str(DATA),
        "KillMode=control-group",
        "TimeoutStartSec=30",
        "TimeoutStopSec=30",
        "RuntimeMaxSec=24h",
        "Restart=on-failure",
        "RestartSec=30",
        "StandardOutput=journal",
        "StandardError=journal",
        "LogNamespace=kf-onlinejudge",
        "LogRateLimitIntervalSec=30s",
        "LogRateLimitBurst=100",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return ("\n".join(lines) + "\n").encode()


def configuration(receipt: dict, release_id: str) -> bytes:
    validate_receipt(receipt)
    return proxy.render(
        release_id, {"schema_version": 1, "origin": receipt["origin"], **receipt["tls"]}
    )
