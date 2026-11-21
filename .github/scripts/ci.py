"""CI and release helpers shared by every workflow (DR-0018: no PowerShell).

Each subcommand is one step's logic, so ci.yml, release.yml and pr-gate.yml
run the same checks on Windows, macOS and Linux with only Python's standard
library. Failures raise SystemExit with a message; GitHub shows it as the
step's error.
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
EXE = ".exe" if PLATFORM == "windows" else ""
RELEASE_DIR = "code-intel-pipeline"
CAPABILITY_SCHEMA = "code-intel-sentrux-capability-artifact.v2"

# Pinned tool releases. orchestration/toolchain-versions.v1.json reads the
# ast-grep version from this line; keep each version a single literal.
RG_VERSION = "15.2.0"
AST_GREP_VERSION = "0.45.3"
TOOLS: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "rg": (
        RG_VERSION,
        {
            "windows": (
                f"ripgrep-{RG_VERSION}-x86_64-pc-windows-msvc.zip",
                "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5",
            ),
            "macos": (
                f"ripgrep-{RG_VERSION}-aarch64-apple-darwin.tar.gz",
                "3750b2e93f37e0c692657da574d7019a101c0084da05a790c83fd335bad973e4",
            ),
            "linux": (
                f"ripgrep-{RG_VERSION}-x86_64-unknown-linux-musl.tar.gz",
                "33e15bcf1624b25cdd2a55813a47a2f95dbe126268203e76aa6a585d1e7b149c",
            ),
        },
    ),
    "ast-grep": (
        AST_GREP_VERSION,
        {
            "windows": (
                "app-x86_64-pc-windows-msvc.zip",
                "3751b7d6be7fd39a80df1180ffe7e053903dcf92d3190e5a8336ff1746af9059",
            ),
            "macos": (
                "app-aarch64-apple-darwin.zip",
                "6d2279dea5bea2ad79c66ea93f5fe54ba926e398a8a26de76c56db68fe59eac6",
            ),
            "linux": (
                "app-x86_64-unknown-linux-gnu.zip",
                "f8ac830881339d1edee6b2652f54798c0f4da5a827f2db38a08ee31117783ce8",
            ),
        },
    ),
}
TOOL_URLS = {
    "rg": "https://github.com/BurntSushi/ripgrep/releases/download/{version}/{asset}",
    "ast-grep": "https://github.com/ast-grep/ast-grep/releases/download/{version}/{asset}",
}


def fail(message: str) -> None:
    # A workflow-command annotation is readable from the checks API; the raw
    # job log is not.
    encoded = message.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::error title=ci.py::{encoded}", flush=True)
    raise SystemExit(f"error: {message}")


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    print("+ " + subprocess.list2cmdline(command), flush=True)
    return subprocess.run(command, text=True, encoding="utf-8", errors="replace", **kwargs)


def check(command: list[str], what: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    completed = run(command, **kwargs)
    if completed.returncode != 0:
        fail(f"{what} failed with exit code {completed.returncode}")
    return completed


def github_file(variable: str, line: str) -> None:
    target = os.environ.get(variable)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def version_of(binary: str, pattern: str) -> tuple[int, ...] | None:
    try:
        output = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return None
    match = re.search(pattern, output)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


# --- install-tool -----------------------------------------------------------


def install_tool(args: argparse.Namespace) -> None:
    name = args.tool
    version, assets = TOOLS[name]
    wanted = tuple(int(part) for part in version.split("."))
    pattern = r"^ripgrep (\d+\.\d+\.\d+)" if name == "rg" else r"ast-grep (\d+\.\d+\.\d+)"
    existing = shutil.which(name)
    # DR-0002: the rg pin is a floor; ast-grep must match exactly.
    if existing and name == "rg" and (version_of(existing, pattern) or ()) >= wanted:
        print(f"{name} already present at {existing} (floor {version})")
        return
    if PLATFORM == "macos" and platform.machine() != "arm64":
        fail("only Apple Silicon macOS runners are pinned")
    asset, expected = assets[PLATFORM]
    directory = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / name
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / asset
    url = TOOL_URLS[name].format(version=version, asset=asset)
    for attempt in range(3):
        try:
            urllib.request.urlretrieve(url, archive)
            break
        except OSError as error:
            if attempt == 2:
                fail(f"download {url}: {error}")
    actual = sha256(archive)
    if actual != expected:
        fail(f"{name} artifact digest mismatch: {actual}")
    if asset.endswith(".zip"):
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(directory)
    else:
        with tarfile.open(archive) as handle:
            handle.extractall(directory, filter="data")
    binary_name = ("rg" if name == "rg" else "ast-grep") + EXE
    binary = next(directory.rglob(binary_name), None)
    if binary is None:
        fail(f"{binary_name} not found in {asset}")
    binary.chmod(0o755)
    if version_of(str(binary), pattern) != wanted:
        fail(f"{name} version mismatch at {binary}")
    github_file("GITHUB_PATH", str(binary.parent))
    print(f"installed {name} {version} -> {binary}")


# --- package ----------------------------------------------------------------


def add_zip_entry(archive: zipfile.ZipFile, name: str, data: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3  # Unix, so bootstrap restores the mode bits
    info.external_attr = (0o100000 | mode) << 16
    archive.writestr(info, data)


def package(args: argparse.Namespace) -> None:
    """Zip `git archive HEAD` plus the release binary, never the working tree."""
    binary = ROOT / "target" / "release" / f"code-intel{EXE}"
    if not binary.is_file():
        fail(f"release binary missing: {binary}")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / f"code-intel-pipeline-{args.tag}-{PLATFORM}.zip"
    tarball = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, capture_output=True, check=True
    ).stdout
    with zipfile.ZipFile(zip_path, "w") as archive, tarfile.open(
        fileobj=io.BytesIO(tarball)
    ) as tree:
        for member in tree.getmembers():
            if member.isfile():
                data = tree.extractfile(member).read()  # type: ignore[union-attr]
                add_zip_entry(archive, f"{RELEASE_DIR}/{member.name}", data, member.mode & 0o777)
        add_zip_entry(archive, f"{RELEASE_DIR}/bin/code-intel{EXE}", binary.read_bytes(), 0o755)
    digest = sha256(zip_path)
    Path(f"{zip_path}.sha256").write_text(f"{digest} *{zip_path.name}\n", encoding="ascii")
    toolchain = subprocess.run(["rustc", "--version"], capture_output=True, text=True, cwd=ROOT)
    manifest = {
        "schema": "code-intel-release-manifest.v1",
        "tag": args.tag,
        "commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT, check=True
        ).stdout.strip(),
        "zipSha256": digest,
        "toolchain": toolchain.stdout.strip(),
        "builtAt": datetime.now(timezone.utc).isoformat(),
    }
    Path(f"{zip_path}.release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="ascii"
    )
    github_file("GITHUB_OUTPUT", f"platform={PLATFORM}")
    github_file("GITHUB_OUTPUT", f"zip={zip_path}")
    print(f"packaged {zip_path} sha256={digest}")


# --- install-package --------------------------------------------------------


def install_package(args: argparse.Namespace) -> None:
    """Install a packaged zip exactly as a user would: through the Skill bootstrap."""
    zip_path = Path(args.zip).resolve()
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
    required = [
        "skills/code-intel-pipeline/SKILL.md",
        "skills/code-intel-pipeline/agents/openai.yaml",
        "skills/code-intel-pipeline/scripts/bootstrap.py",
        f"bin/code-intel{EXE}",
    ]
    missing = [path for path in required if f"{RELEASE_DIR}/{path}" not in names]
    if missing:
        fail(f"release package is missing required files: {missing}")
    install_root = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / f"code-intel-install-{args.tag}"
    completed = check(
        [
            sys.executable,
            str(ROOT / "skills/code-intel-pipeline/scripts/bootstrap.py"),
            "--local-asset", str(zip_path),
            "--version", args.tag,
            "--repo-path", str(Path(args.repo_path).resolve()),
            "--install-root", str(install_root),
            "--json",
        ],
        "bootstrap install",
        capture_output=True,
    )
    result = json.loads(completed.stdout)
    if result.get("status") not in ("installed", "repaired", "already_installed"):
        fail(f"unexpected bootstrap status: {completed.stdout}")
    Path(args.report).write_text(json.dumps(result, indent=2), encoding="utf-8")
    setup = result["setup"]
    print(f"bootstrap {result['status']} {result['tag']} -> {result['release_root']}")
    for hint in result.get("missing_tools", []):
        print(f"::warning::missing {hint['tool']}: {hint['install']}")
    github_file("GITHUB_PATH", setup["paths"]["bin"])
    github_file("GITHUB_OUTPUT", f"release_root={result['release_root']}")
    github_file("GITHUB_OUTPUT", f"release_bin={Path(result['release_root']) / 'bin'}")


def installed_report(path: str) -> tuple[dict[str, Any], Path, Path]:
    report = read_json(Path(path))
    release_root = Path(report["release_root"]).resolve()
    binary = Path(report["setup"]["paths"]["bin"]) / f"code-intel{EXE}"
    return report, release_root, binary


# --- installed-checks -------------------------------------------------------


def installed_checks(args: argparse.Namespace) -> None:
    """The installed binary finds itself as the graph provider, and setup's
    PATH and CODE_INTEL_HOME reach a fresh shell."""
    report, release_root, binary = installed_report(args.report)
    observation = run(
        [str(binary), "doctor", "bootstrap", "--pipeline-root", str(release_root),
         "--repo-path", str(ROOT), "--no-require-repowise", "--json"],
        capture_output=True,
    )
    if observation.returncode not in (0, 1):
        fail(f"installed doctor failed with exit code {observation.returncode}: {observation.stderr}")
    graph = json.loads(observation.stdout)["checks"]["graphProvider"]
    if not graph.get("binaryFound"):
        fail(f"installed doctor did not find its graph binary: {graph}")
    packaged = release_root / "bin" / f"code-intel{EXE}"
    if Path(graph["binaryPath"]).resolve() != packaged:
        fail(f"installed doctor resolved {graph['binaryPath']} instead of {packaged}")
    print(f"installed doctor ({binary}) found the packaged graph binary {packaged}")

    bin_dir = str(Path(report["setup"]["paths"]["bin"]))
    home = report["setup"]["paths"]["codeIntelHome"]
    if PLATFORM == "windows":
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            user_path, path_type = winreg.QueryValueEx(key, "Path")
            persisted_home, _ = winreg.QueryValueEx(key, "CODE_INTEL_HOME")
        entries = [entry.rstrip("\\").lower() for entry in user_path.split(";")]
        if bin_dir.rstrip("\\").lower() not in entries:
            fail(f"user Path lacks {bin_dir}: {user_path}")
        if path_type != winreg.REG_EXPAND_SZ:
            fail("user Path was downgraded from REG_EXPAND_SZ")
        if Path(persisted_home).resolve() != Path(home).resolve():
            fail(f"CODE_INTEL_HOME is {persisted_home}, expected {home}")
        print(f"user environment: {bin_dir} on Path, CODE_INTEL_HOME={persisted_home}")
        return
    # A fresh shell with a minimal environment must pick both up from the
    # startup files setup edited, with no manual profile step.
    shells = [["bash", "-l", "-c"]] + ([["zsh", "-c"]] if shutil.which("zsh") else [])
    for shell in shells:
        probe = run(
            shell + ['printf "%s\\n%s\\n" "$PATH" "$CODE_INTEL_HOME"'],
            capture_output=True,
            env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"},
        )
        lines = probe.stdout.splitlines()
        if len(lines) < 2 or bin_dir not in lines[-2].split(":") or lines[-1] != home:
            fail(f"fresh {shell[0]} shell lacks setup's environment:\n{probe.stdout}{probe.stderr}")
        print(f"fresh {shell[0]} shell: {bin_dir} on PATH, CODE_INTEL_HOME={home}")


# --- sentrux-closure --------------------------------------------------------


def sentrux_closure(args: argparse.Namespace) -> None:
    """Run the pipeline and accept it only through its committed artifacts:
    the completion marker, the content-addressed manifest and every Sentrux
    capability ref the artifact index re-admits. Stdout is never authority."""
    binary = Path(args.binary).resolve()
    if not binary.is_file():
        binary = binary.with_name(binary.name + EXE)
    repo = Path(args.repo).resolve()
    work = Path(args.work).resolve()
    staging, authority = work / "staging", work / "authority"
    authority.mkdir(parents=True, exist_ok=True)
    final = args.final_name
    completed = run(
        [str(binary), "run", "execute", "--repo", str(repo), "--out", str(staging),
         "--authority-root", str(authority), "--final-name", final,
         "--manifest", str(repo / "orchestration/integrations.json"),
         "--doctor-require-repowise", "false"],
        capture_output=True,
    )
    print(completed.stdout, completed.stderr, sep="\n", flush=True)
    if completed.returncode != 0:
        try:
            failures = json.dumps(json.loads(completed.stdout).get("failures"), indent=1)
        except (json.JSONDecodeError, AttributeError):
            failures = completed.stderr[-2000:]
        fail(
            f"run execute failed with exit code {completed.returncode} (10 = architecture/domain "
            f"gate failure, 70 = process failure, 73 = --final-name already published, 76 = "
            f"budget stopped):\n{failures}"
        )
    index_path = authority / "index.json"
    check([str(binary), "artifact", "index", "--artifact-root", str(authority),
           "--output", str(index_path), "--operation", "rebuild"], "artifact index rebuild")

    run_root = authority / repo.name / final
    marker = read_json(run_root / "run-complete.json")
    if marker.get("schema") != "code-intel-run-commit.v1":
        fail("run completion marker is invalid")
    manifest = read_json(run_root / marker["manifest"]["path"])
    if manifest.get("schema") != "code-intel-run-manifest.v1" or manifest.get("outcome") != "completed":
        fail("run manifest is not completed")
    snapshot = marker["snapshotIdentity"]
    if manifest.get("snapshotIdentity") != snapshot:
        fail("completion marker and run manifest snapshots differ")
    if args.require_pass:
        node = manifest.get("nodes", {}).get("evidence.sentrux", {})
        if node.get("status") != "succeeded" or node.get("verdict") != "pass":
            fail(f"Sentrux evidence node is not a passing verified result: {node}")

    entries = [
        entry for entry in read_json(index_path).get("entries", [])
        if entry.get("repo") == repo.name and entry.get("run") == final
    ]
    if len(entries) != 1:
        fail("run was not admitted exactly once by the artifact index")
    entry = entries[0]
    if entry.get("outcome") != "completed" or entry.get("snapshotIdentity") != snapshot:
        fail("artifact index identity does not match the completion marker")
    refs = entry.get("artifactRefs", [])
    if sum(1 for ref in refs if ref.get("type") == "diagnosis.hospital") != 1:
        fail("run is missing its verified hospital report ref")
    capability_refs = [
        ref for ref in refs
        if ref.get("artifactSchema") == CAPABILITY_SCHEMA
        and ref.get("type") == "provider.sentrux.capability-artifact"
    ]
    if not capability_refs:
        fail("run has no verified Sentrux capability refs")

    matrix = read_json(repo / "orchestration/sentrux-capability-matrix.v1.json")
    by_id = {capability["id"]: capability for capability in matrix["capabilities"]}
    observed: dict[str, dict[str, Any]] = {}
    for ref in capability_refs:
        if ref["path"] != f"objects/sha256/{ref['sha256']}":
            fail(f"capability ref is not content-addressed: {ref['path']}")
        object_path = run_root / ref["path"]
        if not object_path.is_file() or sha256(object_path) != ref["sha256"]:
            fail(f"capability object is missing or its digest differs: {ref['path']}")
        payload = read_json(object_path)
        capability_id = payload.get("capabilityId")
        if payload.get("schema") != CAPABILITY_SCHEMA or payload.get("snapshotIdentity") != snapshot:
            fail(f"capability artifact is not snapshot-bound: {ref['path']}")
        if capability_id not in by_id:
            fail(f"capability is absent from the matrix: {capability_id}")
        if by_id[capability_id]["operation"] != payload.get("operation"):
            fail(f"capability operation disagrees with the matrix: {capability_id}")
        if capability_id in observed:
            fail(f"duplicate capability artifact: {capability_id}")
        if payload.get("status") != "succeeded" and payload.get("failure") is None:
            fail(f"non-success capability has no explicit failure: {capability_id}")
        observed[capability_id] = payload

    required = [capability for capability in matrix["capabilities"] if capability.get("requiredForRelease")]
    if not required:
        fail("capability matrix exposes no required capabilities")
    for capability in required:
        capability_id = capability["id"]
        payload = observed.get(capability_id)
        if payload is None:
            fail(f"required Sentrux capability is missing: {capability_id}")
        status, mode, state = payload.get("status"), capability.get("executionMode"), capability.get("currentState")
        if mode == "automatic":
            accepted = {"authoritative_automatic": {"succeeded"}, "automatic_degraded": {"succeeded", "degraded"}}
            if state not in accepted:
                fail(f"automatic capability has an unsupported matrix state: {capability_id} ({state})")
            if status not in accepted[state]:
                fail(f"automatic capability {capability_id} ({state}) ended {status}")
        elif mode in ("explicit_authority", "lifecycle_external"):
            if status != "not_applicable":
                fail(f"explicit/lifecycle capability must be not_applicable: {capability_id} ({status})")
            if not str((payload.get("failure") or {}).get("kind") or "").strip():
                fail(f"explicit/lifecycle capability has no failure reason: {capability_id}")
        else:
            fail(f"capability has no recognized execution mode: {capability_id} ({mode})")
    print(
        f"Sentrux closure verified from committed refs: {len(capability_refs)} capability refs; "
        f"required={len(required)}; matrix coverage={matrix.get('coverageStatus')}"
    )

    # ai-safety-003 (#34): an audit report anywhere in the scanned snapshot or
    # the run output must pass the fail-closed validator before it can be
    # treated as authoritative.
    reports = {path.resolve() for root in (repo, staging) if root.is_dir()
               for path in root.rglob("audit-report.json")}
    for report in sorted(reports):
        check([str(binary), "audit", "--operation", "validate", "--repo", str(repo),
               "--report", str(report)], f"audit report validation of {report}")
    if not reports:
        print("no audit-report.json in the snapshot or run output")


# --- secret-guard -----------------------------------------------------------


SECRET_PATTERNS = [
    re.compile(pattern)
    for pattern in (
        r"sk-[A-Za-z0-9_-]{20,}",
        r"ghp_[A-Za-z0-9_]{20,}",
        r"github_pat_[A-Za-z0-9_]{20,}",
        r"AKIA[0-9A-Z]{16}",
    )
]


def secret_guard(_args: argparse.Namespace) -> None:
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode().split("\0")
    hits = []
    for name in filter(None, files):
        if re.match(r"^(target/|artifacts/|cache/)", name) or re.search(r"\.(png|jpg|jpeg|gif|zip|exe)$", name):
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits += [f"{name} matches {pattern.pattern}" for pattern in SECRET_PATTERNS if pattern.search(text)]
    if hits:
        print("\n".join(hits))
        fail("secret-like patterns found")
    print(f"no secret-like patterns in {len(files)} tracked files")


# --- quality-summary --------------------------------------------------------


def shown(value: Any) -> str:
    """A projection value as the summary prints it: no Python repr leaks."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def quality_summary(args: argparse.Namespace) -> None:
    """Render the Quality Signal projection as the job summary, a sticky
    comment body and annotations. The projection itself comes from the
    compiled CLI (#386); this only formats it."""
    projection = read_json(Path(args.projection))
    signal = projection["qualitySignal"]
    total = signal["total"]
    delta = total.get("delta")
    admission = projection.get("admission") or {}
    policy = admission.get("policy") or {}
    policy_text = f"{policy['policyId']} v{policy.get('policyVersion')}" if policy.get("policyId") else "unbound"
    regression = signal.get("regression")
    if isinstance(regression, dict):
        regression = regression.get("status")

    def short(value: Any, length: int) -> str:
        return str(value or "")[:length]

    lines = [
        "### Code Intel Quality Signal",
        "",
        f"Completeness: **{projection.get('completeness')}** &middot; snapshot "
        f"`{short(projection.get('snapshotIdentity'), 12)}` &middot; commit "
        f"`{short(projection.get('binding', {}).get('commit'), 12)}`",
        f"Admission: **{admission.get('verdict') or 'unknown'}** &middot; policy **{policy_text}** "
        f"&middot; aggregate regression: **{shown(regression)}** (advisory, not a blocking gate)",
        "",
        "| Total | Baseline | Delta |",
        "|---|---|---|",
        f"| {shown(total.get('current'))} | {shown(total.get('baseline'))} | "
        f"{'[DOWN]' if delta is not None and delta < 0 else '[OK]'} {'n/a' if delta is None else delta} |",
        "",
        f"Bottleneck: **{signal.get('bottleneck') or 'none'}**",
        "",
        "| Root cause | Baseline | Current | Delta |",
        "|---|---|---|---|",
    ]
    for cause in signal.get("rootCauses") or []:
        raw = cause.get("raw") or {}
        raw_delta = "n/a" if raw.get("delta") is None else round(float(raw["delta"]), 2)
        lines.append(f"| {cause.get('label')} | {shown(raw.get('baseline'))} | {shown(raw.get('current'))} | {raw_delta} |")
    lines.append("")
    findings = projection.get("findings") or []
    if findings:
        lines += [
            f"**Findings** ({len(findings)} total)",
            "",
            "| Kind | Severity | Disposition | Rule | Message |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {f.get('kind')} | {f.get('severityNormalized')} | {f.get('disposition')} | "
            f"`{f.get('rule')}` | {f.get('message')} |"
            for f in findings[:10]
        ]
        lines.append("")
    lines += [f"> {diagnostic}" for diagnostic in projection.get("diagnostics") or []]
    text = "\n".join(lines) + "\n"
    github_file("GITHUB_STEP_SUMMARY", text)
    Path(args.comment).write_text("<!-- code-intel-quality-signal -->\n" + text, encoding="utf-8")
    for finding in findings:
        label = f"[{finding.get('rule')}]: {finding.get('message')}"
        if finding.get("disposition") == "advisory":
            print(f"::warning::Quality Signal advisory {label}")
        elif finding.get("severityNormalized") in ("high", "critical"):
            targets = finding.get("targets") or []
            suffix = f" ({', '.join(targets)})" if targets else ""
            print(f"::error::Quality Signal {finding.get('kind')} {label}{suffix}")
        else:
            print(f"::warning::Quality Signal {finding.get('kind')} {label}")


# --- verify-inventory -------------------------------------------------------


def verify_inventory(args: argparse.Namespace) -> None:
    """Exactly one zip, checksum sidecar and manifest per platform, with real digests."""
    directory = Path(args.dir)
    entries = list(directory.iterdir())
    if len(entries) != 9 or any(entry.is_dir() for entry in entries):
        fail(f"release inventory must be nine flat files, found {sorted(e.name for e in entries)}")
    for target in ("windows", "macos", "linux"):
        zip_path = directory / f"code-intel-pipeline-{args.tag}-{target}.zip"
        sidecar = Path(f"{zip_path}.sha256")
        manifest_path = Path(f"{zip_path}.release-manifest.json")
        for path in (zip_path, sidecar, manifest_path):
            if not path.is_file():
                fail(f"release asset is missing: {path}")
        digest = sha256(zip_path)
        expected = f"{digest} *{zip_path.name}"
        if sidecar.read_text(encoding="ascii").strip() != expected:
            fail(f"checksum sidecar mismatch for {zip_path}: expected '{expected}'")
        manifest = read_json(manifest_path)
        if (
            manifest.get("schema") != "code-intel-release-manifest.v1"
            or manifest.get("tag") != args.tag
            or manifest.get("zipSha256") != digest
            or (args.commit and manifest.get("commit") != args.commit)
        ):
            fail(f"release manifest identity/digest mismatch for {zip_path}")
        print(f"verified {zip_path.name} sha256={digest}")


# --- configs release --------------------------------------------------------


CONFIGS_BUNDLE_STEM = "code-intel-configs"
CONFIGS_VERSION_FILE = ROOT / "configs" / "version.json"
CONFIGS_VERIFIER = ROOT / "scripts" / "verify_configs_release.py"
CONFIGS_ACCEPTANCE_SCHEMA = "code-intel-configs-acceptance.v1"
# The committed configuration surface a `configs-v*` release certifies. Rust
# source, tests, docs and the host-local factory/eval assets are not
# configuration: a consumer pins this set, not the implementation.
# `pipeline.config.json` carries the producer's own `repos` overrides; a
# consumer replaces those with its own and is told so in the release notes.
CONFIGS_SURFACE = (
    "orchestration",
    "overlays",
    "pipeline.config.example.json",
    "pipeline.config.json",
    "skills",
    "templates",
)


def git_stdout(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def configs_document() -> dict[str, Any]:
    """The one source of truth for the configs version and its Code Intel range."""
    if not CONFIGS_VERSION_FILE.is_file():
        fail(f"missing {CONFIGS_VERSION_FILE.relative_to(ROOT)}")
    document = read_json(CONFIGS_VERSION_FILE)
    if document.get("schema") != "code-intel-configs-version.v1":
        fail(f"configs/version.json has an unsupported schema: {document.get('schema')!r}")
    version = document.get("configsVersion")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        fail(f"configs/version.json declares an invalid configsVersion: {version!r}")
    compat = document.get("codeIntel")
    if not isinstance(compat, dict) or not isinstance(compat.get("minVersion"), str):
        fail("configs/version.json declares no codeIntel.minVersion")
    return document


def configs_verifier() -> Any:
    """Load the consumer verifier so one implementation owns the compatibility gate."""
    if not CONFIGS_VERIFIER.is_file():
        fail(f"missing {CONFIGS_VERIFIER.relative_to(ROOT)}")
    spec = importlib.util.spec_from_file_location("verify_configs_release", CONFIGS_VERIFIER)
    if spec is None or spec.loader is None:
        fail(f"cannot load {CONFIGS_VERIFIER.relative_to(ROOT)}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configs_archive(version: str) -> tuple[bytes, list[dict[str, Any]]]:
    """A reproducible tar.gz of the committed configuration surface, plus its inventory."""
    prefix = f"{CONFIGS_BUNDLE_STEM}-{version}"
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *CONFIGS_SURFACE],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout.decode("utf-8", "surrogateescape")
    paths = sorted(name for name in listed.split("\0") if name)
    if not paths:
        fail("the configured configuration surface contains no tracked files")
    tarball = subprocess.run(
        ["git", "archive", "--format=tar", f"--prefix={prefix}/", "HEAD", "--", *paths],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    buffer = io.BytesIO()
    # `mtime=0` and no source name keep the gzip header reproducible, and the
    # members keep git's own metadata, so two runs on one commit agree.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as stream:
        stream.write(tarball)
    archive = buffer.getvalue()
    members: list[dict[str, Any]] = []
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tree:
        for member in tree.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                fail(f"the configuration surface contains a non-regular member: {member.name}")
            relative = member.name[len(prefix) + 1 :]
            if not relative or relative.startswith("/") or ".." in Path(relative).parts:
                fail(f"the configuration surface contains an unsafe member name: {member.name}")
            data = tree.extractfile(member).read()  # type: ignore[union-attr]
            members.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
    if len(members) != len(paths):
        fail(f"the archive holds {len(members)} files but git lists {len(paths)}")
    return archive, members


def package_configs(args: argparse.Namespace) -> None:
    """Assemble the `configs-v*` inventory: bundle, manifest, sidecar, extra assets.

    Everything is built from `git archive HEAD`, never the working tree, so no
    earlier CI step can change what ships. The manifest digests every bundled
    file; the sidecar then digests the bundle, the manifest and every extra
    asset, so a consumer can detect drift without trusting this script.
    """
    document = configs_document()
    version = document["configsVersion"]
    match = re.fullmatch(r"configs-v([0-9]+\.[0-9]+\.[0-9]+)", args.tag)
    if not match:
        fail(f"unsupported tag: {args.tag} (expected configs-v<semver>)")
    if match.group(1) != version:
        fail(f"tag {args.tag} does not match the declared configsVersion {version}")
    archive, members = configs_archive(version)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    bundle_path = out / f"{CONFIGS_BUNDLE_STEM}-{version}.tar.gz"
    bundle_path.write_bytes(archive)

    assets: list[dict[str, Any]] = []
    for item in args.asset or []:
        source = Path(item)
        if not source.is_file():
            fail(f"--asset {item} is not a file")
        target = out / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        if target.name in {bundle_path.name, f"{CONFIGS_BUNDLE_STEM}-{version}.manifest.json"}:
            fail(f"--asset {item} collides with a generated asset name")
        assets.append(
            {"name": target.name, "sha256": sha256(target), "bytes": target.stat().st_size}
        )

    manifest = {
        "schema": "code-intel-configs-manifest.v1",
        "configsVersion": version,
        "tag": args.tag,
        "producer": {
            "commit": git_stdout("rev-parse", "HEAD"),
            "tree": git_stdout("rev-parse", "HEAD^{tree}"),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "workflow": ".github/workflows/configs-release.yml",
        },
        "codeIntel": document["codeIntel"],
        "bundle": {
            "name": bundle_path.name,
            "sha256": sha256(bundle_path),
            "bytes": len(archive),
            "fileCount": len(members),
            "files": members,
        },
        "assets": assets,
    }
    manifest_path = out / f"{CONFIGS_BUNDLE_STEM}-{version}.manifest.json"
    manifest_bytes = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    manifest_path.write_bytes(manifest_bytes)

    sidecar = [f"{sha256(bundle_path)} *{bundle_path.name}"]
    sidecar.append(f"{hashlib.sha256(manifest_bytes).hexdigest()} *{manifest_path.name}")
    sidecar += [f"{entry['sha256']} *{entry['name']}" for entry in assets]
    (out / f"{CONFIGS_BUNDLE_STEM}-{version}.sha256").write_text(
        "\n".join(sidecar) + "\n", encoding="ascii"
    )

    github_file("GITHUB_OUTPUT", f"tag={args.tag}")
    github_file("GITHUB_OUTPUT", f"bundle={bundle_path}")
    github_file("GITHUB_OUTPUT", f"manifest={manifest_path}")
    print(
        f"packaged {bundle_path.name} ({len(members)} files, sha256={manifest['bundle']['sha256']}) "
        f"with {len(assets)} extra asset(s)"
    )


def extract_configs_bundle(bundle: Path, target: Path) -> None:
    """Expand the bundle for a run, refusing anything but regular files with safe names."""
    target.mkdir(parents=True, exist_ok=False)
    with tarfile.open(bundle, mode="r:gz") as tree:
        for member in tree.getmembers():
            if member.isdir():
                continue
            relative = member.name.split("/", 1)[1] if "/" in member.name else ""
            if (
                not member.isfile()
                or not relative
                or relative.startswith("/")
                or ".." in Path(relative).parts
            ):
                fail(f"bundle contains an unsafe member: {member.name}")
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(tree.extractfile(member).read())  # type: ignore[union-attr]


def configs_acceptance(args: argparse.Namespace) -> None:
    """Accept the release contract from a consumer's side.

    Three things have to hold at once: the extracted bundle is the configuration
    the binary actually resolves (`orchestrate validate` names the manifest it
    read), a normal-mode run on a representative repository completes with its
    readable artifacts, and an evidence payload between the historical 8 MiB
    default and the 64 MiB contract budget is admitted while one beyond the
    budget is still refused.
    """
    document = configs_document()
    compat = document["codeIntel"]
    verifier = configs_verifier()
    fixture = Path(args.fixture).resolve()
    request_template = read_json(fixture / "evidence-request.json")
    executable = args.binary
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "schema": CONFIGS_ACCEPTANCE_SCHEMA,
        "tag": args.tag or f"configs-v{document['configsVersion']}",
        "producer": {"commit": git_stdout("rev-parse", "HEAD")},
        "acceptedAt": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }

    version = verifier.binary_version(executable)
    satisfies, reason = verifier.compatibility_verdict(version, compat)
    report["producer"]["codeIntelVersion"] = version
    checks.append(
        {"name": "code-intel-version", "ok": satisfies, "detail": f"{executable}: {reason}"}
    )

    env = os.environ.copy()
    env.pop("CODE_INTEL_INTEGRATIONS_MANIFEST", None)
    repo_dir = work / "repo"
    shutil.copytree(fixture / "repo", repo_dir)
    for command in (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.name=code-intel-configs-ci",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "-qm",
            "configs baseline fixture",
        ],
    ):
        completed = subprocess.run(command, cwd=repo_dir, capture_output=True, text=True)
        if completed.returncode != 0:
            fail(f"fixture preparation failed ({' '.join(command)}): {completed.stderr.strip()}")

    if args.bundle:
        config_home = work / "config-home"
        extract_configs_bundle(Path(args.bundle).resolve(), config_home)
        env["CODE_INTEL_HOME"] = str(config_home)
        plan = subprocess.run(
            [
                executable,
                "orchestrate",
                "validate",
                "--action",
                "Validate",
                "--repo",
                str(repo_dir),
                "--json",
            ],
            cwd=work,
            capture_output=True,
            text=True,
            env=env,
        )
        resolved = None
        try:
            resolved = json.loads(plan.stdout).get("manifest")
        except json.JSONDecodeError:
            resolved = None
        expected = str((config_home / "orchestration" / "integrations.json").resolve())
        checks.append(
            {
                "name": "configuration-home",
                "ok": plan.returncode == 0 and resolved == expected,
                "detail": f"resolved={resolved} expected={expected}",
            }
        )
    else:
        checks.append(
            {
                "name": "configuration-home",
                "ok": False,
                "detail": "no --bundle was given, so the run would validate the build checkout",
            }
        )

    run_dir = work / "normal"
    run_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [executable, str(repo_dir), "--mode", "normal", "--json"],
        cwd=run_dir,
        capture_output=True,
        text=True,
        env=env,
    )
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        result = None
    result = result or {}
    checks.append(
        {
            "name": "normal-run",
            "ok": completed.returncode == 0
            and result.get("outcome") == "completed"
            and result.get("exitCode") == 0
            and result.get("failureNode") is None,
            "detail": (
                f"exit={completed.returncode} outcome={result.get('outcome')} "
                f"failureNode={result.get('failureNode')} diagnostic={result.get('diagnostic')}"
            ),
        }
    )
    readable = result.get("readableArtifacts") or {}
    missing = [
        name
        for name in ("agentCodeSliceRanking", "hospitalMarkdown")
        if not Path(str(readable.get(name) or "")).is_file()
    ]
    checks.append(
        {
            "name": "normal-run-readable-artifacts",
            "ok": bool(readable) and not missing,
            "detail": f"missing={missing} readable={sorted(readable)}",
        }
    )

    def evidence_case(name: str, payload: bytes) -> None:
        case = work / name
        case.mkdir(parents=True, exist_ok=True)
        request = json.loads(json.dumps(request_template))
        request["observation"]["payload"]["sha256"] = hashlib.sha256(payload).hexdigest()
        (case / "payload.json").write_bytes(payload)
        (case / "request.json").write_text(
            json.dumps(request, separators=(",", ":")), encoding="utf-8"
        )
        run = subprocess.run(
            [
                executable,
                "evidence",
                "validate",
                "--request",
                str(case / "request.json"),
                "--artifact-root",
                str(case),
            ],
            cwd=work,
            capture_output=True,
            text=True,
        )
        try:
            outcome = json.loads(run.stdout)
        except json.JSONDecodeError:
            outcome = {}
        checks.append(
            {
                "name": name,
                "ok": run.returncode == 0
                and outcome.get("status") == "admitted"
                and outcome.get("domainVerdict") == "observed",
                "detail": (
                    f"payload={len(payload)} bytes exit={run.returncode} "
                    f"status={outcome.get('status')} "
                    f"diagnostics={(outcome.get('diagnostics') or run.stderr.strip())[:200]}"
                ),
            }
        )

    evidence_case("evidence-payload-10MiB", b'{"schema":"code-intel-evidence-payload.v1","data":{"padding":"' + b"a" * (10 * 1024 * 1024) + b'"}}')

    oversize = work / "evidence-payload-oversize"
    oversize.mkdir(parents=True, exist_ok=True)
    oversized = b'{"schema":"code-intel-evidence-payload.v1","data":{"padding":"' + b"a" * (64 * 1024 * 1024 + 1024) + b'"}}'
    request = json.loads(json.dumps(request_template))
    request["observation"]["payload"]["sha256"] = hashlib.sha256(oversized).hexdigest()
    (oversize / "payload.json").write_bytes(oversized)
    (oversize / "request.json").write_text(json.dumps(request, separators=(",", ":")), encoding="utf-8")
    run = subprocess.run(
        [
            executable,
            "evidence",
            "validate",
            "--request",
            str(oversize / "request.json"),
            "--artifact-root",
            str(oversize),
        ],
        cwd=work,
        capture_output=True,
        text=True,
    )
    try:
        outcome = json.loads(run.stdout)
    except json.JSONDecodeError:
        outcome = {}
    checks.append(
        {
            "name": "evidence-payload-beyond-contract",
            "ok": run.returncode != 0 and outcome.get("status") != "admitted",
            "detail": (
                f"payload={len(oversized)} bytes exit={run.returncode} "
                f"status={outcome.get('status')} stderr={run.stderr.strip()[:200]}"
            ),
        }
    )

    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failed = [check for check in checks if not check["ok"]]
    for check in failed:
        print(f"::error title=configs-acceptance::{check['name']}: {check['detail']}")
    if failed:
        fail("configs acceptance failed: " + ", ".join(check["name"] for check in failed))
    print(f"configs acceptance passed: {len(checks)} checks, code-intel {version}")


# --- release-tag ------------------------------------------------------------


def release_tag(args: argparse.Namespace) -> None:
    """The tag is v<Cargo version>; GA (non beta/rc) tags must be signed."""
    match = re.fullmatch(r"v([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)", args.tag)
    if not match:
        fail(f"unsupported release tag: {args.tag}")
    cargo = (ROOT / "crates/code-intel-cli/Cargo.toml").read_text(encoding="utf-8")
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', cargo)
    if not version or version.group(1) != match.group(1):
        fail(f"release tag {args.tag} does not match Cargo package version {version and version.group(1)}")
    if re.search(r"-(beta|rc)\.[0-9]+$", args.tag):
        print(f"pre-release tag {args.tag}: signed-tag gate skipped")
        return
    check(["git", "config", "gpg.ssh.allowedSignersFile", ".github/allowed_signers"], "git config", cwd=ROOT)
    check(["git", "verify-tag", args.tag], f"signature verification of {args.tag} (see docs/RELEASE_SIGNING.md)", cwd=ROOT)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("install-tool")
    command.add_argument("tool", choices=sorted(TOOLS))
    command.set_defaults(handler=install_tool)

    command = commands.add_parser("package")
    command.add_argument("--tag", required=True)
    command.add_argument("--out", default="dist")
    command.set_defaults(handler=package)

    command = commands.add_parser("install-package")
    command.add_argument("--zip", required=True)
    command.add_argument("--tag", required=True)
    command.add_argument("--repo-path", default=".")
    command.add_argument("--report", required=True)
    command.set_defaults(handler=install_package)

    command = commands.add_parser("installed-checks")
    command.add_argument("--report", required=True)
    command.set_defaults(handler=installed_checks)

    command = commands.add_parser("sentrux-closure")
    command.add_argument("--binary", required=True)
    command.add_argument("--repo", default=".")
    command.add_argument("--work", required=True)
    command.add_argument("--final-name", required=True)
    command.add_argument("--require-pass", action="store_true")
    command.set_defaults(handler=sentrux_closure)

    command = commands.add_parser("secret-guard")
    command.set_defaults(handler=secret_guard)

    command = commands.add_parser("quality-summary")
    command.add_argument("--projection", required=True)
    command.add_argument("--comment", required=True)
    command.set_defaults(handler=quality_summary)

    command = commands.add_parser("verify-inventory")
    command.add_argument("--dir", default="dist")
    command.add_argument("--tag", required=True)
    command.add_argument("--commit")
    command.set_defaults(handler=verify_inventory)
    command = commands.add_parser("package-configs")
    command.add_argument("--tag", required=True)
    command.add_argument("--out", default="dist")
    command.add_argument("--asset", action="append", default=[])
    command.set_defaults(handler=package_configs)

    command = commands.add_parser("configs-acceptance")
    command.add_argument("--binary", required=True)
    command.add_argument("--bundle")
    command.add_argument("--fixture", default="tests/fixtures/configs-baseline")
    command.add_argument("--work", required=True)
    command.add_argument("--out", required=True)
    command.add_argument("--tag")
    command.set_defaults(handler=configs_acceptance)


    command = commands.add_parser("release-tag")
    command.add_argument("--tag", required=True)
    command.set_defaults(handler=release_tag)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
