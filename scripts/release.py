#!/usr/bin/env python3
"""Generate release notes and a clean AstrBot plugin archive from a version tag."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import zipfile
from datetime import date
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "astrbot_plugin_cnb_bot"
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
METADATA_VERSION_RE = re.compile(
    r"(?m)^version:\s*['\"]?(\d+\.\d+\.\d+)['\"]?\s*$"
)
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^)]+\))?(?P<breaking>!)?:\s*(?P<subject>.+)$",
    re.IGNORECASE,
)

CATEGORIES = {
    "feat": "新功能",
    "fix": "问题修复",
    "perf": "性能优化",
    "docs": "文档",
    "refactor": "维护与重构",
    "build": "维护与重构",
    "ci": "维护与重构",
    "test": "维护与重构",
    "chore": "维护与重构",
    "style": "维护与重构",
    "revert": "维护与重构",
}
BREAKING_CATEGORY = "破坏性变更"
OTHER_CATEGORY = "其他变更"


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def version_tuple(value: str) -> tuple[int, int, int] | None:
    match = TAG_RE.fullmatch(value if value.startswith("v") else f"v{value}")
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def validate_tag(tag: str) -> tuple[int, int, int]:
    parsed = version_tuple(tag)
    if parsed is None or not tag.startswith("v"):
        raise ValueError(f"标签格式必须为 vMAJOR.MINOR.PATCH，例如 v0.2.1：{tag}")

    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    version_match = METADATA_VERSION_RE.search(metadata)
    if not version_match:
        raise ValueError("metadata.yaml 中没有有效的三段式 version 字段。")
    if version_match.group(1) != tag[1:]:
        raise ValueError(
            f"版本不一致：标签是 {tag}，metadata.yaml 是 {version_match.group(1)}。"
        )

    try:
        tag_commit = git("rev-parse", "--verify", f"{tag}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Git 标签不存在：{tag}") from exc
    head_commit = git("rev-parse", "HEAD")
    if tag_commit != head_commit:
        raise ValueError(f"当前 HEAD 与标签 {tag} 指向的提交不一致。")
    return parsed


def previous_release_tag(tag: str, current_version: tuple[int, int, int]) -> str | None:
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for candidate in git("tag", "--merged", tag).splitlines():
        parsed = version_tuple(candidate)
        if candidate != tag and parsed is not None and parsed < current_version:
            candidates.append((parsed, candidate))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]


def release_notes(tag: str, previous_tag: str | None) -> str:
    date_text = git("show", "-s", "--format=%cs", tag) or date.today().isoformat()
    revision_range = f"{previous_tag}..{tag}" if previous_tag else tag
    lines = git(
        "log",
        "--no-merges",
        "--format=%H%x09%s",
        revision_range,
    ).splitlines()
    sections: dict[str, list[str]] = {}

    for line in lines:
        commit_hash, separator, subject = line.partition("\t")
        if not separator or not subject:
            continue
        match = CONVENTIONAL_RE.match(subject)
        if match:
            category = (
                BREAKING_CATEGORY
                if match.group("breaking")
                else CATEGORIES.get(match.group("type").lower(), OTHER_CATEGORY)
            )
            display_subject = match.group("subject").strip()
        else:
            category = OTHER_CATEGORY
            display_subject = subject.strip()
        sections.setdefault(category, []).append(
            f"- {display_subject} (`{commit_hash[:7]}`)"
        )

    order = [
        BREAKING_CATEGORY,
        "新功能",
        "问题修复",
        "性能优化",
        "文档",
        "维护与重构",
        OTHER_CATEGORY,
    ]
    result = [f"# {PLUGIN_NAME} {tag} ({date_text})", ""]
    result.append(f"变更范围：`{revision_range}`。" if previous_tag else "首次版本，包含该标签可达的提交。")
    result.append("")
    found_changes = False
    for category in order:
        entries = sections.get(category)
        if not entries:
            continue
        found_changes = True
        result.extend([f"## {category}", "", *entries, ""])
    if not found_changes:
        result.extend(["## 变更", "", "- 此版本没有可列出的提交。", ""])
    return "\n".join(result).rstrip() + "\n"


def plugin_files(tag: str) -> list[str]:
    paths = git("ls-tree", "-r", "--name-only", tag).splitlines()
    excluded_dirs = {".github", "scripts", "tests", "dist"}
    excluded_names = {".env", ".DS_Store", ".gitignore", "CHANGELOG.md"}
    files = []
    for path in paths:
        parts = PurePosixPath(path).parts
        if not parts or parts[0] in excluded_dirs or path in excluded_names:
            continue
        if any(part in excluded_dirs for part in parts):
            continue
        if Path(path).name in excluded_names:
            continue
        source = ROOT / path
        if source.is_symlink():
            raise ValueError(f"插件包不支持符号链接：{path}")
        if not source.is_file():
            raise ValueError(f"标签文件在工作区缺失：{path}")
        files.append(path)

    required = {"__init__.py", "metadata.yaml", "main.py", "_conf_schema.json"}
    missing = required.difference(files)
    if missing:
        raise ValueError("插件包缺少必要文件：" + ", ".join(sorted(missing)))
    return sorted(files)


def create_archive(tag: str, output_dir: Path) -> Path:
    archive = output_dir / f"{PLUGIN_NAME}-{tag}.zip"
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as package:
        for relative_path in plugin_files(tag):
            info = zipfile.ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            package.writestr(info, (ROOT / relative_path).read_bytes())
    return archive


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_release(tag: str, output_dir: Path) -> list[Path]:
    current_version = validate_tag(tag)
    previous_tag = previous_release_tag(tag, current_version)
    output_dir.mkdir(parents=True, exist_ok=True)

    changelog = output_dir / f"CHANGELOG-{tag}.md"
    changelog.write_text(release_notes(tag, previous_tag), encoding="utf-8")
    archive = create_archive(tag, output_dir)
    checksums = output_dir / "SHA256SUMS.txt"
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in (archive, changelog)),
        encoding="utf-8",
    )
    return [archive, changelog, checksums]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a release tag, generate changelog notes, and package the plugin."
    )
    parser.add_argument("tag", help="Release tag matching metadata.yaml, e.g. v0.2.1")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    for artifact in build_release(args.tag, output_dir):
        print(artifact.relative_to(ROOT) if artifact.is_relative_to(ROOT) else artifact)


if __name__ == "__main__":
    main()
