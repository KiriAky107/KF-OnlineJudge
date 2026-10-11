"""Stricter Web package declarations; the accepted local CLI format remains unchanged."""

from dataclasses import dataclass

from kf_oj.contracts.judging import decode_wire
from kf_oj.modules.problems.import_format import ImportEnvelope, ProblemImport, ProblemImportV2

ARCHIVE_SUFFIXES = (".zip", ".jar", ".war", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar")


@dataclass(frozen=True)
class WebDeclaration:
    manifest: ProblemImport | ProblemImportV2
    members: frozenset[str]


def decode_web_declaration(data: bytes) -> WebDeclaration:
    # decode_wire rejects duplicate JSON keys, unknown fields and noncanonical
    # protocol values before either local paths or node policy can be considered.
    manifest = decode_wire(ImportEnvelope, data).root
    hidden = {name for c in manifest.cases for name in (c.input_file, c.expected_file)}
    samples = {name for s in manifest.samples for name in (s.input_file, s.output_file)}
    metadata = {"problem.json", manifest.statement_file}
    if len(metadata) != 2 or hidden & (samples | metadata) or samples & metadata:
        raise ValueError("public and hidden package declarations must be separate")
    members = frozenset(metadata | hidden | samples)
    if len({name.lower() for name in members}) != len(members):
        raise ValueError("package names must be case-insensitively unique")
    if any(name.lower().endswith(ARCHIVE_SUFFIXES) for name in members):
        raise ValueError("nested archive declarations are unsupported")
    return WebDeclaration(manifest, members)
