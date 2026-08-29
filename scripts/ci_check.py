#!/usr/bin/env python3
"""Fail-closed lint/build/package checks for beamo-flasher-releases.

This public repository stores GitHub Release download assets, not
application source. There is no GitHub Actions workflow or test suite
to mirror. Packaging manifests (Homebrew, Scoop, Chocolatey) depend on
a public MIT LICENSE URL in this repo, so CI still has to fail if that
contract or the Cloud Build config is broken.

Does not publish releases, overwrite GitHub release assets, or deploy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_GCP_PROJECT = "social-media-499020"
OFFICIAL_BUILDER_PREFIX = "gcr.io/cloud-builders/"

REQUIRED_FILES = (
    "LICENSE",
    "README.md",
    "cloudbuild.yaml",
    "scripts/ci_check.py",
)

FORBIDDEN_SECRET_NAMES = (
    ".env",
    ".env.local",
    "credentials.json",
    "service-account.json",
    "sa.json",
    "id_rsa",
    "id_ed25519",
)

SKIP_DIR_NAMES = {".git", "__pycache__", ".pytest_cache"}

FORBIDDEN_CLOUDBUILD_KEYS = (
    "availableSecrets",
    "secretEnv",
    "secrets",
    "images",
    "artifacts",
)

FORBIDDEN_ARG_PATTERNS = (
    re.compile(r"\bgcloud\s+deploy\b"),
    re.compile(r"\bgcloud\s+run\s+deploy\b"),
    re.compile(r"\bgcloud\s+app\s+deploy\b"),
    re.compile(r"\bgh\s+release\b"),
    re.compile(r"\bgithub\.com/.+/releases\b"),
    re.compile(r"\bupload-release-asset\b"),
    re.compile(r"\boverwrite\b"),
)

MIT_MARKERS = (
    "MIT License",
    "Permission is hereby granted, free of charge",
    "THE SOFTWARE IS PROVIDED \"AS IS\"",
)


class CheckError(Exception):
    pass


def _fail(message: str) -> None:
    raise CheckError(message)


def load_yaml_mapping(text: str, source: str) -> dict[str, Any]:
    """Parse a constrained YAML 1.1 mapping used by cloudbuild.yaml.

    Stdlib only. Fails closed on tabs, empty documents, and constructs
    this helper does not understand.
    """
    if "\t" in text:
        _fail(f"{source}: tabs are not allowed in Cloud Build YAML")

    lines: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.lstrip(" ")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)
        if indent % 2 != 0:
            _fail(f"{source}:{lineno}: indent must be a multiple of 2")
        if stripped.startswith("---") or stripped.startswith("..."):
            _fail(f"{source}:{lineno}: YAML document markers are not supported")
        lines.append((indent, stripped))

    if not lines:
        _fail(f"{source}: document is empty")

    value, next_index = _parse_yaml_value(lines, 0, 0, source)
    if next_index != len(lines):
        _fail(f"{source}: trailing content after top-level mapping")
    if not isinstance(value, dict):
        _fail(f"{source}: top-level value must be a mapping")
    return value


def _parse_yaml_value(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
    source: str,
) -> tuple[Any, int]:
    if index >= len(lines):
        _fail(f"{source}: unexpected end of document")
    line_indent, content = lines[index]
    if line_indent != indent:
        _fail(f"{source}: indent mismatch")
    if content.startswith("- "):
        return _parse_yaml_list(lines, index, indent, source)
    return _parse_yaml_map(lines, index, indent, source)


def _parse_yaml_map(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
    source: str,
) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent != indent:
            _fail(f"{source}: unexpected indent while parsing mapping")
        if content.startswith("- "):
            _fail(f"{source}: list item found where mapping entry expected")
        key, sep, rest = content.partition(":")
        if not sep or not key or key.strip() != key or " " in key:
            _fail(f"{source}: invalid mapping key {content!r}")
        rest = rest.strip()
        index += 1
        if rest:
            result[key] = _parse_yaml_scalar(rest, source)
            continue
        if index >= len(lines) or lines[index][0] <= indent:
            result[key] = {}
            continue
        child_indent = lines[index][0]
        if child_indent <= indent:
            _fail(f"{source}: nested value for {key!r} has invalid indent")
        value, index = _parse_yaml_value(lines, index, child_indent, source)
        result[key] = value
    return result, index


def _parse_yaml_list(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
    source: str,
) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        line_indent, content = lines[index]
        if line_indent < indent:
            break
        if line_indent != indent or not content.startswith("- "):
            _fail(f"{source}: expected list item")
        item = content[2:].strip()
        index += 1
        if not item:
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_yaml_value(
                    lines, index, lines[index][0], source
                )
                result.append(value)
            else:
                result.append(None)
            continue
        if item.endswith(":") and not _is_quoted_scalar(item):
            key = item[:-1]
            nested: dict[str, Any] = {}
            if index < len(lines) and lines[index][0] > indent:
                value, index = _parse_yaml_value(
                    lines, index, lines[index][0], source
                )
                nested[key] = value
            else:
                nested[key] = {}
            extra, index = _parse_yaml_map_continuations(
                lines, index, indent + 2, source
            )
            nested.update(extra)
            result.append(nested)
            continue
        if ":" in item and not item.startswith(("'", '"')):
            key, sep, rest = item.partition(":")
            if sep and key and key.strip() == key and " " not in key:
                nested = {key: _parse_yaml_scalar(rest.strip(), source)}
                extra, index = _parse_yaml_map_continuations(
                    lines, index, indent + 2, source
                )
                nested.update(extra)
                result.append(nested)
                continue
        result.append(_parse_yaml_scalar(item, source))
    return result, index


def _parse_yaml_map_continuations(
    lines: list[tuple[int, str]],
    index: int,
    indent: int,
    source: str,
) -> tuple[dict[str, Any], int]:
    if index >= len(lines) or lines[index][0] < indent:
        return {}, index
    if lines[index][0] == indent and not lines[index][1].startswith("- "):
        return _parse_yaml_map(lines, index, indent, source)
    return {}, index


def _is_quoted_scalar(value: str) -> bool:
    return (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    )


def _parse_yaml_scalar(value: str, source: str) -> Any:
    if value in {"|", ">", "|-", "|+", ">-", ">+"}:
        _fail(f"{source}: multiline YAML scalars are not allowed")
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def collect_arg_strings(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(collect_arg_strings(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(collect_arg_strings(item))
    return found


def check_required_files(root: Path) -> None:
    for relative in REQUIRED_FILES:
        path = root / relative
        if not path.is_file():
            _fail(f"missing required file: {relative}")
        if path.stat().st_size == 0:
            _fail(f"required file is empty: {relative}")


def check_license(root: Path) -> None:
    text = (root / "LICENSE").read_text(encoding="utf-8")
    for marker in MIT_MARKERS:
        if marker not in text:
            _fail(f"LICENSE is not a complete MIT license (missing {marker!r})")
    if "BeamoINT" not in text:
        _fail("LICENSE must retain the BeamoINT copyright holder")


def check_readme(root: Path) -> None:
    text = (root / "README.md").read_text(encoding="utf-8").strip()
    if len(text) < 40:
        _fail("README.md is too short to describe this release repository")


def _is_skipped(relative: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in relative.parts) or relative.suffix == ".pyc"


def check_no_secrets(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _is_skipped(relative):
            continue
        if path.name in FORBIDDEN_SECRET_NAMES:
            _fail(f"refusing to proceed with secret-like file: {relative}")


def check_json_files(root: Path) -> None:
    for path in root.rglob("*.json"):
        relative = path.relative_to(root)
        if _is_skipped(relative):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            _fail(f"invalid JSON in {relative}: {exc}")


def check_cloudbuild_config(root: Path) -> dict[str, Any]:
    path = root / "cloudbuild.yaml"
    raw = path.read_text(encoding="utf-8")
    if EXPECTED_GCP_PROJECT not in raw:
        _fail(
            "cloudbuild.yaml must name GCP project "
            f"{EXPECTED_GCP_PROJECT}"
        )
    config = load_yaml_mapping(raw, "cloudbuild.yaml")
    for key in FORBIDDEN_CLOUDBUILD_KEYS:
        if key in config:
            _fail(f"cloudbuild.yaml must not set {key} (no secrets/publish/deploy)")
    steps = config.get("steps")
    if not isinstance(steps, list) or not steps:
        _fail("cloudbuild.yaml must define a non-empty steps list")
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            _fail(f"cloudbuild.yaml steps[{index}] must be a mapping")
        name = step.get("name")
        if not isinstance(name, str) or not name.startswith(OFFICIAL_BUILDER_PREFIX):
            _fail(
                f"cloudbuild.yaml steps[{index}].name must be an official "
                f"builder image under {OFFICIAL_BUILDER_PREFIX}"
            )
        for key in ("secretEnv", "availableSecrets"):
            if key in step:
                _fail(f"cloudbuild.yaml steps[{index}] must not set {key}")
        for argument in collect_arg_strings(step.get("args")):
            for pattern in FORBIDDEN_ARG_PATTERNS:
                if pattern.search(argument):
                    _fail(
                        f"cloudbuild.yaml steps[{index}] looks like a "
                        f"publish/deploy command: {argument!r}"
                    )
    timeout = config.get("timeout")
    if not isinstance(timeout, str) or not re.fullmatch(r"\d+s", timeout):
        _fail("cloudbuild.yaml timeout must be a duration like 600s")
    options = config.get("options")
    if not isinstance(options, dict):
        _fail("cloudbuild.yaml must set options.logging: CLOUD_LOGGING_ONLY")
    logging = options.get("logging")
    if logging != "CLOUD_LOGGING_ONLY":
        _fail(
            "cloudbuild.yaml options.logging must be CLOUD_LOGGING_ONLY "
            "(required when Cloud Build triggers inject a user-specified "
            "service account)"
        )
    if "serviceAccount" in config or "serviceAccount" in options:
        _fail(
            "cloudbuild.yaml must not set serviceAccount; Cloud Build "
            "triggers inject the user-specified service account"
        )
    return config


def check_project_id() -> None:
    project_id = os.environ.get("PROJECT_ID")
    if project_id is None:
        return
    if project_id != EXPECTED_GCP_PROJECT:
        _fail(
            f"PROJECT_ID={project_id!r} is not {EXPECTED_GCP_PROJECT}; "
            "do not run this CI in any other GCP project"
        )


def package_checksums(root: Path) -> str:
    """Build a SHA256SUMS-style package of tracked repo files."""
    files = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and not _is_skipped(path.relative_to(root))
    ]
    if not files:
        _fail("package check found no files")
    lines: list[str] = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        relative = path.relative_to(root).as_posix()
        lines.append(f"{digest}  {relative}")
    return "\n".join(lines) + "\n"


def run_checks(root: Path) -> str:
    check_required_files(root)
    check_license(root)
    check_readme(root)
    check_no_secrets(root)
    check_json_files(root)
    check_cloudbuild_config(root)
    check_project_id()
    return package_checksums(root)


def run_self_test() -> None:
    """Prove the checker fails closed when required config is broken."""
    with tempfile.TemporaryDirectory(prefix="flasher-releases-ci-") as tmp:
        tmp_root = Path(tmp)
        for relative in REQUIRED_FILES:
            source = REPO_ROOT / relative
            dest = tmp_root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())

        run_checks(tmp_root)

        (tmp_root / "LICENSE").write_text("not a license\n", encoding="utf-8")
        try:
            run_checks(tmp_root)
        except CheckError:
            pass
        else:
            _fail("self-test: truncated LICENSE should have failed")
        (tmp_root / "LICENSE").write_bytes((REPO_ROOT / "LICENSE").read_bytes())

        broken = (tmp_root / "cloudbuild.yaml").read_text(encoding="utf-8")
        broken = broken.replace(OFFICIAL_BUILDER_PREFIX, "docker.io/library/")
        (tmp_root / "cloudbuild.yaml").write_text(broken, encoding="utf-8")
        try:
            run_checks(tmp_root)
        except CheckError:
            pass
        else:
            _fail("self-test: unofficial builder image should have failed")

        (tmp_root / "cloudbuild.yaml").write_bytes(
            (REPO_ROOT / "cloudbuild.yaml").read_bytes()
        )
        no_logging = (tmp_root / "cloudbuild.yaml").read_text(encoding="utf-8")
        no_logging = no_logging.replace("CLOUD_LOGGING_ONLY", "LEGACY")
        (tmp_root / "cloudbuild.yaml").write_text(no_logging, encoding="utf-8")
        try:
            run_checks(tmp_root)
        except CheckError:
            pass
        else:
            _fail("self-test: missing CLOUD_LOGGING_ONLY should have failed")

        (tmp_root / "cloudbuild.yaml").write_bytes(
            (REPO_ROOT / "cloudbuild.yaml").read_bytes()
        )
        (tmp_root / "credentials.json").write_text("{}", encoding="utf-8")
        try:
            run_checks(tmp_root)
        except CheckError:
            pass
        else:
            _fail("self-test: secret-like file should have failed")

    print("self-test: fail-closed checks passed")


def main(argv: list[str]) -> int:
    if argv == ["--self-test"]:
        try:
            run_self_test()
        except CheckError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
        return 0
    if argv:
        print(f"usage: {Path(__file__).name} [--self-test]", file=sys.stderr)
        return 2
    try:
        checksums = run_checks(REPO_ROOT)
    except CheckError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("lint: required files, MIT LICENSE, README, Cloud Build config OK")
    print("build: cloudbuild.yaml uses official gcr.io/cloud-builders images")
    print("package: SHA256SUMS of in-repo files (not published)")
    sys.stdout.write(checksums)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
