#!/usr/bin/env python3
"""Verify a published `configs-v*` configuration baseline from a clean environment.

Standard library only: a consumer needs Python and nothing else. The script

  1. downloads the release assets for one tag into a directory that must be
     empty (or reads an already assembled directory), so a stale file can never
     stand in for a verified one;
  2. checks the SHA-256 sidecar against the manifest and every asset, and the
     bundle's per-file digests against the manifest;
  3. resolves the tag to a commit through the GitHub API, compares it with
     `producer.commit`, then compares every bundled file byte-for-byte with the
     repository content at that commit — which is what binds the assets to a
     source revision rather than merely to each other;
  4. refuses a Code Intel binary outside the compatibility range the manifest
     declares, with a diagnostic naming both versions.

Trust model (docs/configs-release.md): digests prove integrity against damage
and drift; the source comparison proves which repository revision produced the
bundle; the compatibility gate fails closed and never silently accepts an older
binary. This is integrity plus source binding, not authenticated provenance —
for that, verify the build-provenance attestation with `gh attestation verify`.

Usage:
    python scripts/verify_configs_release.py --tag configs-v0.1.0
    python scripts/verify_configs_release.py --tag configs-v0.1.0 \
        --dir /tmp/fresh --code-intel "$(command -v code-intel)"
    python scripts/verify_configs_release.py --tag configs-v0.1.0 \
        --dist dist --expect-commit "$GITHUB_SHA"

Exit codes: 0 verified, 1 verification failed, 2 usage or environment error.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

MANIFEST_SCHEMA = "code-intel-configs-manifest.v1"
VERIFIER_NAME = "verify_configs_release.py"
ACCEPTANCE_NAME = "acceptance.json"
DEFAULT_REPO = "XartLab/code-intel-pipeline"
DEFAULT_GITHUB = "https://github.com"
DEFAULT_API = "https://api.github.com"
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_SIDECAR_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
TAG_PATTERN = re.compile(r"^configs-v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
VERSION_PATTERN = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]+))?"
)


class VerificationError(Exception):
    """A verification step failed with a message a consumer can act on."""


def parse_version(text: str) -> tuple[tuple[int, int, int], tuple[tuple[int, Any], ...]]:
    """Parse a SemVer version into its release and prerelease parts.

    SemVer precedence (semver.org §11) is what the compatibility gate needs: a
    bare numeric compare puts `0.7.2-beta.10` below `0.7.2-beta.7`, and a
    lexical compare rejects a later prerelease.
    """
    match = VERSION_PATTERN.fullmatch(text.strip())
    if not match:
        raise ValueError(f"not a supported version: {text!r}")
    prerelease: list[tuple[int, Any]] = []
    if match.group(4):
        for identifier in match.group(4).split("."):
            if not identifier:
                raise ValueError(f"empty prerelease identifier in {text!r}")
            if identifier.isdigit():
                if len(identifier) > 1 and identifier[0] == "0":
                    raise ValueError(f"numeric prerelease identifier has a leading zero: {text!r}")
                prerelease.append((0, int(identifier)))
            else:
                prerelease.append((1, identifier))
    release = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return release, tuple(prerelease)


def compare_versions(left: str, right: str) -> int:
    """-1, 0 or 1 under SemVer precedence."""
    left_release, left_pre = parse_version(left)
    right_release, right_pre = parse_version(right)
    if left_release != right_release:
        return -1 if left_release < right_release else 1
    if not left_pre and not right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    for (left_kind, left_value), (right_kind, right_value) in zip(left_pre, right_pre):
        if (left_kind, left_value) == (right_kind, right_value):
            continue
        if left_kind != right_kind:
            return -1 if left_kind < right_kind else 1
        return -1 if left_value < right_value else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


def compatibility_verdict(version: str, block: dict[str, Any]) -> tuple[bool, str]:
    """Whether `version` may consume this baseline, and the reason either way."""
    minimum = block.get("minVersion")
    maximum = block.get("maxExclusive")
    if not isinstance(minimum, str) or not minimum:
        raise VerificationError("manifest declares no codeIntel.minVersion")
    try:
        below_floor = compare_versions(version, minimum) < 0
    except ValueError as error:
        raise VerificationError(
            f"cannot compare {version!r} with the declared minimum {minimum!r}: {error}"
        ) from error
    if below_floor:
        return False, f"{version} is below the declared minimum {minimum}"
    if isinstance(maximum, str) and maximum and compare_versions(version, maximum) >= 0:
        return False, f"{version} is outside the declared range (must be below {maximum})"
    return True, f"{version} is inside [{minimum}, {maximum or 'no published fix yet'})"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, token: str | None, max_bytes: int) -> bytes:
    headers = {"User-Agent": "verify-configs-release", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        detail = error.read(2048).decode("utf-8", "replace")
        raise VerificationError(f"GET {url} failed with HTTP {error.code}: {detail}") from error
    except OSError as error:
        raise VerificationError(f"GET {url} failed: {error}") from error
    if len(body) > max_bytes:
        raise VerificationError(f"GET {url} exceeded {max_bytes} bytes")
    return body


def api_json(url: str, token: str | None) -> dict[str, Any]:
    try:
        value = json.loads(fetch(url, token, MAX_TEXT_BYTES))
    except json.JSONDecodeError as error:
        raise VerificationError(f"GET {url} did not return JSON: {error}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"GET {url} returned {type(value).__name__}, expected an object")
    return value


def resolve_tag_commit(repo: str, tag: str, token: str | None, api: str) -> str:
    """The commit a tag points at, peeling annotated tags."""
    ref = api_json(f"{api}/repos/{repo}/git/ref/tags/{tag}", token)
    object_ = ref.get("object") or {}
    sha, kind = object_.get("sha"), object_.get("type")
    if not isinstance(sha, str) or not sha:
        raise VerificationError(f"tag {tag} has no object sha: {ref}")
    if kind == "commit":
        return sha
    if kind != "tag":
        raise VerificationError(f"tag {tag} points at an unsupported object type: {kind}")
    peeled = api_json(f"{api}/repos/{repo}/git/tags/{sha}", token)
    commit = (peeled.get("object") or {}).get("sha")
    if (peeled.get("object") or {}).get("type") != "commit" or not isinstance(commit, str):
        raise VerificationError(f"annotated tag {tag} does not resolve to a commit: {peeled}")
    return commit


def release_assets(repo: str, tag: str, token: str | None, api: str) -> dict[str, str]:
    release = api_json(f"{api}/repos/{repo}/releases/tags/{tag}", token)
    if release.get("draft"):
        raise VerificationError(f"release {tag} is still a draft, so a consumer cannot rely on it")
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise VerificationError(f"release {tag} exposes no asset list")
    names = [asset.get("name") for asset in assets]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise VerificationError(f"release {tag} has a duplicate or nameless asset")
    urls = {asset["name"]: asset.get("browser_download_url") for asset in assets}
    if any(not url for url in urls.values()):
        raise VerificationError(f"release {tag} has an asset without a download URL")
    return {name: url for name, url in urls.items() if url}


def expected_asset_names(tag: str) -> list[str]:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise VerificationError(f"unsupported tag: {tag} (expected configs-v<semver>)")
    version = ".".join(match.groups())
    return [
        f"code-intel-configs-{version}.tar.gz",
        f"code-intel-configs-{version}.manifest.json",
        f"code-intel-configs-{version}.sha256",
        VERIFIER_NAME,
        ACCEPTANCE_NAME,
    ]


def read_sidecar(path: Path) -> dict[str, str]:
    if path.stat().st_size > MAX_SIDECAR_BYTES:
        raise VerificationError(f"{path.name} is larger than {MAX_SIDECAR_BYTES} bytes")
    entries: dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise VerificationError(f"{path.name} has an unparsable line: {line!r}")
        name = parts[1].strip().lstrip("*")
        if name in entries:
            raise VerificationError(f"{path.name} lists {name} twice")
        entries[name] = parts[0]
    if not entries:
        raise VerificationError(f"{path.name} lists no files")
    return entries


def source_inventory(archive: bytes, prefix: str, tops: set[str]) -> dict[str, bytes]:
    """Every file the repository archive holds under the bundle's top-level paths."""
    inventory: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tree:
        for member in tree.getmembers():
            if not member.name.startswith(prefix):
                continue
            relative = member.name[len(prefix) :]
            if not relative:
                continue
            top = relative.split("/", 1)[0]
            if top not in tops:
                continue
            if member.isdir():
                continue
            if not member.isfile():
                raise VerificationError(
                    f"repository at the producer commit has an unsupported member: {relative}"
                )
            inventory[relative] = tree.extractfile(member).read()  # type: ignore[union-attr]
    return inventory


def compare_with_source(archive: bytes, prefix: str, files: list[dict[str, Any]]) -> int:
    """Match every bundled file against the repository content at the producer commit."""
    if not files:
        raise VerificationError("manifest lists no bundled files, so the source claim is empty")
    tops = {entry["path"].split("/", 1)[0] for entry in files}
    actual = source_inventory(archive, prefix, tops)
    declared = {entry["path"] for entry in files}
    missing, extra = sorted(declared - set(actual)), sorted(set(actual) - declared)
    if missing or extra:
        raise VerificationError(
            "bundle does not match the repository at the producer commit: "
            f"missing from the bundle={missing[:5]} unexpected in the repository={extra[:5]}"
        )
    mismatched = [
        entry["path"]
        for entry in files
        if sha256(actual[entry["path"]]) != entry["sha256"]
    ]
    if mismatched:
        raise VerificationError(
            "bundle files differ from the repository at the producer commit: "
            + ", ".join(mismatched[:5])
        )
    return len(actual)


def verify_bundle(path: Path, bundle: dict[str, Any]) -> list[str]:
    """Re-hash every member inside the bundle against the manifest."""
    problems: list[str] = []
    prefix = f"{bundle['name'][: -len('.tar.gz')]}/"
    try:
        with tarfile.open(path, mode="r:gz") as tree:
            members = tree.getmembers()
            names = [member.name for member in members]
            if len(names) != len(set(names)):
                problems.append(f"{path.name} contains duplicate member paths")
            files: list[dict[str, Any]] = []
            for member in members:
                if member.isdir():
                    continue
                if not member.isfile():
                    problems.append(f"{path.name} contains a non-regular member: {member.name}")
                    continue
                if not member.name.startswith(prefix):
                    problems.append(f"{path.name} has an unexpected member prefix: {member.name}")
                    continue
                data = tree.extractfile(member).read()  # type: ignore[union-attr]
                files.append(
                    {
                        "path": member.name[len(prefix) :],
                        "sha256": sha256(data),
                        "bytes": len(data),
                    }
                )
        declared = {entry["path"]: entry for entry in bundle.get("files", [])}
        actual = {entry["path"]: entry for entry in files}
        for name in sorted(set(declared) - set(actual)):
            problems.append(f"{path.name} is missing the declared file {name}")
        for name in sorted(set(actual) - set(declared)):
            problems.append(f"{path.name} carries the undeclared file {name}")
        for name in sorted(set(declared) & set(actual)):
            if declared[name].get("sha256") != actual[name]["sha256"]:
                problems.append(f"{path.name} member {name} sha256 differs from the manifest")
            elif declared[name].get("bytes") != actual[name]["bytes"]:
                problems.append(f"{path.name} member {name} size differs from the manifest")
        if len(actual) != bundle.get("fileCount"):
            problems.append(
                f"{path.name} holds {len(actual)} files but the manifest declares "
                f"{bundle.get('fileCount')}"
            )
    except (tarfile.TarError, OSError) as error:
        problems.append(f"{path.name} is not a readable tar.gz: {error}")
    return problems


def load_release_files(
    source: str, tag: str, repo: str, token: str | None, api: str, directory: Path
) -> dict[str, Path]:
    names = expected_asset_names(tag)
    if source == "dist":
        if not directory.is_dir():
            raise VerificationError(f"--dist {directory} is not a directory")
        present = sorted(entry.name for entry in directory.iterdir() if entry.is_file())
        if present != sorted(names):
            raise VerificationError(
                "--dist must hold exactly the release assets "
                f"{sorted(names)}, found {present}"
            )
        return {name: directory / name for name in names}
    assets = release_assets(repo, tag, token, api)
    if sorted(assets) != sorted(names):
        raise VerificationError(
            f"release {tag} assets differ from the expected set: "
            f"missing={sorted(set(names) - set(assets))} "
            f"unexpected={sorted(set(assets) - set(names))}"
        )
    directory.mkdir(parents=True, exist_ok=False)
    downloaded: dict[str, Path] = {}
    for name in names:
        target = directory / name
        target.write_bytes(fetch(assets[name], token, MAX_ARCHIVE_BYTES))
        downloaded[name] = target
        print(f"downloaded {name} sha256={sha256(target.read_bytes())}")
    return downloaded


def binary_version(binary: str) -> str:
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, errors="replace", check=False
        )
    except OSError as error:
        raise VerificationError(f"cannot run {binary!r}: {error}") from error
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    found = re.search(
        r"((?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?)",
        output,
    )
    if not found:
        raise VerificationError(
            f"cannot parse a version from {binary!r} output {output!r}; "
            "a binary that does not report code-intel <version> cannot be gated"
        )
    return found.group(1)


def verify(
    tag: str,
    files: dict[str, Path],
    token: str | None,
    api: str,
    github: str,
    repo: str,
    expect_commit: str | None,
    code_intel: str | None,
    require_code_intel: bool,
    source: bool,
) -> dict[str, Any]:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise VerificationError(f"unsupported tag: {tag} (expected configs-v<semver>)")
    version = ".".join(match.groups())
    bundle_name = f"code-intel-configs-{version}.tar.gz"
    manifest_name = f"code-intel-configs-{version}.manifest.json"
    bundle_path, manifest_path = files[bundle_name], files[manifest_name]

    sidecar = read_sidecar(files[f"code-intel-configs-{version}.sha256"])
    for name in (bundle_name, manifest_name):
        if sidecar.get(name) is None:
            raise VerificationError(f"the sidecar does not cover {name}")
    if manifest_path.stat().st_size > MAX_TEXT_BYTES:
        raise VerificationError(f"{manifest_name} is larger than {MAX_TEXT_BYTES} bytes")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise VerificationError(
            f"{manifest_name} has schema {manifest.get('schema')!r}, expected {MANIFEST_SCHEMA}"
        )
    if manifest.get("tag") != tag:
        raise VerificationError(f"{manifest_name} declares tag {manifest.get('tag')!r}, expected {tag!r}")
    if manifest.get("configsVersion") != version:
        raise VerificationError(
            f"{manifest_name} declares configsVersion {manifest.get('configsVersion')!r}, "
            f"expected {version!r} from the tag"
        )

    bundle_bytes = bundle_path.read_bytes()
    bundle_digest = sha256(bundle_bytes)
    bundle = manifest.get("bundle") or {}
    if bundle.get("sha256") != bundle_digest:
        raise VerificationError(
            f"{bundle_name} sha256 is {bundle_digest} but {manifest_name} declares "
            f"{bundle.get('sha256')!r}"
        )
    if sidecar[bundle_name] != bundle_digest:
        raise VerificationError(
            f"{bundle_name} sha256 is {bundle_digest} but the sidecar declares {sidecar[bundle_name]}"
        )
    manifest_digest = sha256(manifest_path.read_bytes())
    if sidecar[manifest_name] != manifest_digest:
        raise VerificationError(
            f"{manifest_name} sha256 is {manifest_digest} but the sidecar declares "
            f"{sidecar[manifest_name]}"
        )

    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise VerificationError(f"{manifest_name} declares no assets")
    declared = {entry.get("name"): entry for entry in assets if isinstance(entry, dict)}
    for name, entry in declared.items():
        path = files.get(name)
        if path is None:
            raise VerificationError(f"{manifest_name} declares asset {name} which the release lacks")
        digest = sha256(path.read_bytes())
        if entry.get("sha256") != digest:
            raise VerificationError(
                f"asset {name} sha256 is {digest} but {manifest_name} declares "
                f"{entry.get('sha256')!r}"
            )
    covered = set(sidecar) - {bundle_name, manifest_name}
    if covered != set(declared):
        raise VerificationError(
            f"the sidecar and {manifest_name} disagree on assets: "
            f"sidecar-only={sorted(covered - set(declared))} "
            f"manifest-only={sorted(set(declared) - covered)}"
        )

    problems = verify_bundle(bundle_path, bundle)
    if problems:
        raise VerificationError("; ".join(problems))

    producer = manifest.get("producer") or {}
    commit = producer.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise VerificationError(f"{manifest_name} has no usable producer.commit: {commit!r}")
    if expect_commit and expect_commit != commit:
        raise VerificationError(f"producer.commit is {commit} but the caller expected {expect_commit}")

    matched: int | None = None
    if source:
        archive = fetch(f"{github}/{repo}/archive/{commit}.tar.gz", token, MAX_ARCHIVE_BYTES)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(archive), mode="rb") as probe:
                probe.read(1)
        except (OSError, EOFError) as error:
            raise VerificationError(f"{repo} archive at {commit} is not readable: {error}") from error
        matched = compare_with_source(archive, f"{repo.split('/')[1]}-{commit}/", bundle.get("files") or [])
        print(f"matched {matched} bundled files against {repo} at {commit}")

    compat = manifest.get("codeIntel") or {}
    report: dict[str, Any] = {
        "tag": tag,
        "version": version,
        "commit": commit,
        "assets": sorted(files),
        "sourceFilesMatched": matched,
        "codeIntel": compat,
        "binary": None,
        "satisfies": None,
        "reason": None,
    }
    if code_intel or require_code_intel:
        binary = code_intel or "code-intel"
        found = binary_version(binary)
        satisfies, reason = compatibility_verdict(found, compat)
        report.update({"binary": found, "satisfies": satisfies, "reason": reason})
        if not satisfies:
            raise VerificationError(
                f"{binary} reports {found}: {reason}. This baseline requires a Code Intel binary "
                f"with the issue 123 fix (minVersion {compat.get('minVersion')}); the configuration "
                "package cannot repair an older installed binary."
            )
    elif not require_code_intel:
        report["reason"] = "no code-intel binary was checked; pass --code-intel to gate the installed one"
    return report


def classify(args: argparse.Namespace) -> int:
    """Render how each candidate version relates to the range declared in a version file."""
    block = json.loads(Path(args.against).read_text(encoding="utf-8")).get("codeIntel") or {}
    rows = []
    for candidate in args.versions:
        try:
            satisfies, reason = compatibility_verdict(candidate, block)
        except (VerificationError, ValueError) as error:
            rows.append({"version": candidate, "satisfies": False, "reason": str(error)})
            continue
        rows.append({"version": candidate, "satisfies": satisfies, "reason": reason})
    print(json.dumps({"codeIntel": block, "versions": rows}, indent=2, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", help="release tag, configs-v<semver>")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"owner/name (default {DEFAULT_REPO})")
    parser.add_argument("--api", default=DEFAULT_API, help=f"GitHub API base (default {DEFAULT_API})")
    parser.add_argument("--github", default=DEFAULT_GITHUB, help=f"GitHub web base (default {DEFAULT_GITHUB})")
    parser.add_argument("--dist", type=Path, help="verify an assembled directory instead of downloading")
    parser.add_argument("--dir", type=Path, help="empty directory to download into")
    parser.add_argument("--expect-commit", help="fail unless producer.commit equals this sha")
    parser.add_argument("--code-intel", help="binary to gate on the declared compatibility range")
    parser.add_argument("--require-code-intel", action="store_true", help="fail when no binary is given")
    parser.add_argument("--no-source", action="store_true", help="skip the repository comparison")
    parser.add_argument("--classify", action="store_true", help="print how --versions relate to --against")
    parser.add_argument("--versions", nargs="*", default=[], help="candidate versions for --classify")
    parser.add_argument("--against", type=Path, help="version file for --classify")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if args.classify:
        if not args.against or not args.versions:
            print("error: --classify needs --against <version.json> and --versions", file=sys.stderr)
            return 2
        try:
            return classify(args)
        except (OSError, json.JSONDecodeError, VerificationError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    if not args.tag:
        print("error: --tag is required", file=sys.stderr)
        return 2
    if bool(args.dist) == bool(args.dir):
        print("error: give exactly one of --dist (assembled) or --dir (download target)", file=sys.stderr)
        return 2

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    directory = (args.dist or args.dir).resolve()  # type: ignore[union-attr]
    try:
        files = load_release_files(
            "dist" if args.dist else "download", args.tag, args.repo, token, args.api, directory
        )
        report = verify(
            args.tag,
            files,
            token,
            args.api,
            args.github,
            args.repo,
            args.expect_commit,
            args.code_intel,
            args.require_code_intel,
            not args.no_source,
        )
    except VerificationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            f"verified {report['tag']} producer={report['commit'][:12]} assets={len(report['assets'])}"
        )
        if report["reason"]:
            print(report["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())