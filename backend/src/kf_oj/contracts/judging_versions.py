"""Version dispatch at storage/transport boundaries; never rewrite legacy snapshots."""

from pydantic import ConfigDict, RootModel

from kf_oj.contracts import judging as v1
from kf_oj.contracts import judging_v2 as v2

Manifest = v1.JobManifest | v2.JobManifest
Report = v1.JudgementReport | v2.JudgementReport


class ManifestEnvelope(RootModel[Manifest]):
    model_config = ConfigDict(frozen=True)


class ReportEnvelope(RootModel[Report]):
    model_config = ConfigDict(frozen=True)


def decode_manifest(payload: bytes) -> Manifest:
    return v1.decode_wire(ManifestEnvelope, payload).root


def decode_report(payload: bytes) -> Report:
    return v1.decode_wire(ReportEnvelope, payload).root


def manifest_bytes(manifest: Manifest) -> bytes:
    return v1.manifest_bytes(manifest)


def validate_report(report: Report, manifest: Manifest, digest: str) -> None:
    if report.schema_version != manifest.schema_version:
        raise ValueError("report protocol differs from authorized manifest")
    report.validate_for_manifest(manifest, digest)
