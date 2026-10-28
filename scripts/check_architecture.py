"""Enforce Python import direction and the Rust internal crate dependency graph."""

import ast
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "backend/src/kf_oj"
RELEASE = ROOT / "scripts/release"
RUST_ALLOWED = {
    "kf-contracts": set(),
    "kf-sandbox": {"kf-contracts"},
    "kf-engine": {"kf-contracts"},
    "kf-coordinator": {"kf-contracts"},
    "kf-worker": {"kf-contracts", "kf-engine", "kf-sandbox"},
}
PYTHON_ALLOWED = {
    "contracts": {"contracts"},
    "core": {"core", "contracts"},
    "infra": {"infra", "core", "contracts"},
    "runtime": {"runtime", "core", "contracts"},
    "modules": {"modules", "runtime", "infra", "core", "contracts"},
    "api": {"api", "modules", "runtime", "infra", "core", "contracts"},
    "workers": {"workers", "modules", "runtime", "infra", "core", "contracts"},
}


def imports(tree: ast.AST, package: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - node.level + 1])
                target = ".".join(filter(None, [base, node.module]))
            else:
                target = node.module or ""
            for alias in node.names:
                yield target if alias.name == "*" else f"{target}.{alias.name}"


def python_violations() -> list[str]:
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if len(relative.parts) < 2:
            continue
        # Offline build tooling composes the API to export its schema, not a runtime dependency.
        if relative.as_posix() == "contracts/export.py":
            continue
        layer = relative.parts[0]
        package = "kf_oj." + ".".join(relative.parts[:-1])
        for target in imports(ast.parse(path.read_text(encoding="utf-8")), package):
            parts = target.split(".")
            if len(parts) < 2 or parts[0] != "kf_oj" or parts[1] == "__version__":
                continue
            if parts[1] not in PYTHON_ALLOWED.get(layer, set()):
                violations.append(f"{path.relative_to(ROOT)}: {layer} cannot import {target}")
            elif layer == "modules" and parts[1] == "modules" and len(parts) >= 3:
                own_module = relative.parts[1]
                if parts[2] != own_module and (len(parts) < 4 or parts[3] != "public"):
                    violations.append(
                        f"{path.relative_to(ROOT)}: cross-module import must use public.py: "
                        f"{target}"
                    )
    return violations


def rust_violations() -> list[str]:
    violations = []
    manifests = list((ROOT / "judge/crates").glob("*/Cargo.toml"))
    found = set()
    for path in manifests:
        manifest = tomllib.loads(path.read_text(encoding="utf-8"))
        name = manifest["package"]["name"]
        found.add(name)
        if name not in RUST_ALLOWED:
            violations.append(f"{path.relative_to(ROOT)}: declare the new crate boundary first")
            continue
        groups = [manifest]
        groups.extend(manifest.get("target", {}).values())
        for group in groups:
            for section in ("dependencies", "dev-dependencies", "build-dependencies"):
                for key, dependency in group.get(section, {}).items():
                    actual = dependency.get("package", key) if isinstance(dependency, dict) else key
                    if actual.startswith("kf-") and actual not in RUST_ALLOWED[name]:
                        violations.append(f"{name} cannot depend on {actual}")
        if name in {"kf-contracts", "kf-engine"}:
            allowed_external = {"serde", "serde_json", "uuid"}
            for key in manifest.get("dependencies", {}):
                if not key.startswith("kf-") and key not in allowed_external:
                    violations.append(f"{name}: domain/contract crate cannot depend on {key}")
    if found != set(RUST_ALLOWED):
        violations.append("Rust workspace crates differ from the declared architecture")
    return violations


def release_violations() -> list[str]:
    """Offline delivery may use public contracts, never application state layers."""
    violations = []
    for path in sorted(RELEASE.glob("*.py")):
        for target in imports(ast.parse(path.read_text(encoding="utf-8")), "scripts.release"):
            if target.startswith("kf_oj.") and not target.startswith("kf_oj.contracts."):
                violations.append(f"{path.name}: release cannot import application state: {target}")
    return violations


def restore_copy_violations() -> list[str]:
    """Keep the fixed offline data plane inside Sandbox and free of execution."""
    root = ROOT / "judge/crates/sandbox"
    manifest = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
    entries = [row for row in manifest.get("bin", []) if row.get("name") == "kf-restore-io"]
    if entries != [{"name": "kf-restore-io", "path": "src/restore_copy/main.rs"}]:
        return ["kf-restore-io must remain the declared offline Sandbox entry"]
    violations = []
    for path in sorted((root / "src/restore_copy").glob("*.rs")):
        text = path.read_text(encoding="utf-8")
        # A source boundary check, complemented by actual Root/cgroup/IO VM tests.
        for forbidden in (
            "Command",
            "std::net",
            "kf_sandbox::",
            "kf_engine::",
            "kf_worker::",
            "kf_coordinator::",
            "libc::",
            "execve",
            "execv(",
            "fork(",
        ):
            if forbidden in text:
                violations.append(f"{path.relative_to(ROOT)}: offline copy cannot use {forbidden}")
    return violations


def main() -> None:
    violations = (
        python_violations() + rust_violations() + release_violations() + restore_copy_violations()
    )
    if violations:
        print("\n".join(violations), file=sys.stderr)
        raise SystemExit(1)
    print("Architecture boundaries: OK (Python imports + Rust crate graph + offline delivery)")


if __name__ == "__main__":
    main()
