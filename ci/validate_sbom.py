"""Validate that CI emitted a structurally complete CycloneDX dependency graph."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast


def validate(path: Path) -> dict[str, int | str]:
    payload = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(payload, dict):
        raise ValueError("SBOM must contain a JSON object")
    document = cast(dict[str, Any], payload)
    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.5":
        raise ValueError("SBOM must be CycloneDX 1.5")
    components = document.get("components")
    dependencies = document.get("dependencies")
    if not isinstance(components, list) or not components:
        raise ValueError("SBOM components are missing")
    if not isinstance(dependencies, list) or not dependencies:
        raise ValueError("SBOM dependency graph is missing")
    component_items = cast(list[object], components)
    dependency_items = cast(list[object], dependencies)

    refs: set[str] = set()
    missing_purl: list[str] = []
    for raw_component in component_items:
        if not isinstance(raw_component, dict):
            raise ValueError("SBOM component must be an object")
        component = cast(dict[str, Any], raw_component)
        name = str(component.get("name", "")).strip()
        version = str(component.get("version", "")).strip()
        reference = str(component.get("bom-ref", "")).strip()
        if not name or not version or not reference:
            raise ValueError("SBOM component lacks name, version, or bom-ref")
        refs.add(reference)
        if not str(component.get("purl", "")).strip():
            missing_purl.append(name)
    if missing_purl:
        raise ValueError("SBOM components lack purl: " + ", ".join(sorted(missing_purl)))

    graph_refs = {
        str(cast(dict[str, Any], item).get("ref", ""))
        for item in dependency_items
        if isinstance(item, dict)
    }
    if not refs <= graph_refs:
        raise ValueError("SBOM dependency graph omits component references")
    return {
        "status": "pass",
        "component_count": len(component_items),
        "dependency_node_count": len(dependency_items),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sbom", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = validate(args.sbom)
    rendered = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
