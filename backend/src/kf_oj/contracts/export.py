"""Export deterministic public contracts without opening DB or queue connections."""

import argparse
import json
from pathlib import Path

from kf_oj.api.app import create_app
from kf_oj.contracts.judge import JudgeTaskNotification
from kf_oj.core.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if checked-in files are stale")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[4]
    schema = JudgeTaskNotification.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "urn:kf-oj:judge-task:v1"
    outputs = {
        root / "contracts/schemas/judge-task.v1.schema.json": schema,
        root / "contracts/openapi/platform.v1.json": create_app(
            Settings(_env_file=None, environment="test")
        ).openapi(),
    }
    stale = []
    for path, document in outputs.items():
        content = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(root)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            print(f"Exported {path.relative_to(root)}")
    if stale:
        parser.exit(1, "Stale contracts: " + ", ".join(stale) + "; run make contracts\n")


if __name__ == "__main__":
    main()
