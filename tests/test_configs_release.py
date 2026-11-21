"""Contract tests for the `configs-v*` release line (issue #389).

Run with: python tests/test_configs_release.py -v

They exercise the real packager and the real consumer verifier against a
throwaway directory, so a change to either side that breaks the other fails
here rather than in a release job.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_SCRIPT = ROOT / ".github" / "scripts" / "ci.py"
VERIFIER_SCRIPT = ROOT / "scripts" / "verify_configs_release.py"
WORKFLOW = ROOT / ".github" / "workflows" / "configs-release.yml"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ci = load(CI_SCRIPT, "code_intel_ci_under_test")
verifier = load(VERIFIER_SCRIPT, "verify_configs_release_under_test")


def version() -> str:
    return ci.configs_document()["configsVersion"]


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


class PackagedInventory(unittest.TestCase):
    """A packaged inventory, in a temporary directory, shared by the cases below."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temporary.name)
        cls.tag = f"configs-v{version()}"
        ci.package_configs(
            argparse.Namespace(
                tag=cls.tag, out=str(cls.directory / "dist"), asset=[str(VERIFIER_SCRIPT)]
            )
        )
        # The consumer verifier only accepts an inventory that also carries the
        # acceptance report; a stand-in with the right name is enough here.
        (cls.directory / "dist" / "acceptance.json").write_text(
            json.dumps({"schema": ci.CONFIGS_ACCEPTANCE_SCHEMA, "checks": []}), encoding="utf-8"
        )
        ci.package_configs(
            argparse.Namespace(
                tag=cls.tag,
                out=str(cls.directory / "dist"),
                asset=[str(VERIFIER_SCRIPT), str(cls.directory / "dist" / "acceptance.json")],
            )
        )
        cls.dist = cls.directory / "dist"
        cls.manifest = json.loads(
            next(cls.dist.glob("code-intel-configs-*.manifest.json")).read_text(encoding="utf-8")
        )
        cls.bundle = next(cls.dist.glob("code-intel-configs-*.tar.gz"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def verify(self, directory: Path, *extra: str) -> int:
        return verifier.main(
            [
                "--tag",
                self.tag,
                "--dist",
                str(directory),
                "--no-source",
                "--expect-commit",
                git("rev-parse", "HEAD"),
                *extra,
            ]
        )

    def copy_dist(self) -> Path:
        target = Path(tempfile.mkdtemp())
        shutil.copytree(self.dist, target, dirs_exist_ok=True)
        self.addCleanup(shutil.rmtree, target, True)
        return target

    def test_manifest_names_the_tag_and_the_producer_commit(self) -> None:
        self.assertEqual(self.manifest["schema"], "code-intel-configs-manifest.v1")
        self.assertEqual(self.manifest["tag"], self.tag)
        self.assertEqual(self.manifest["producer"]["commit"], git("rev-parse", "HEAD"))
        self.assertEqual(self.manifest["codeIntel"]["minVersion"], "0.7.2-beta.7")

    def test_every_bundled_file_is_hashed_and_counted(self) -> None:
        prefix = f"{self.bundle.name[: -len('.tar.gz')]}/"
        with tarfile.open(self.bundle, mode="r:gz") as tree:
            members = [member for member in tree.getmembers() if member.isfile()]
            actual = {}
            for member in members:
                data = tree.extractfile(member).read()
                actual[member.name[len(prefix) :]] = (
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                )
        declared = {entry["path"]: (entry["sha256"], entry["bytes"]) for entry in self.manifest["bundle"]["files"]}
        self.assertEqual(declared, actual)
        self.assertEqual(self.manifest["bundle"]["fileCount"], len(actual))

    def test_the_surface_this_release_certifies_is_not_empty(self) -> None:
        paths = {entry["path"] for entry in self.manifest["bundle"]["files"]}
        for required in ("orchestration/integrations.json", "skills/code-intel-pipeline/SKILL.md"):
            self.assertIn(required, paths)
        for excluded in ("crates/code-intel-cli/src/main.rs", "docs/configs-release.md"):
            self.assertFalse(any(path.startswith(excluded) for path in paths), excluded)

    def test_the_assembled_inventory_verifies(self) -> None:
        self.assertEqual(self.verify(self.dist), 0)

    def test_a_missing_asset_fails(self) -> None:
        directory = self.copy_dist()
        (directory / "acceptance.json").unlink()
        self.assertEqual(self.verify(directory), 1)

    def test_an_undeclared_asset_fails(self) -> None:
        directory = self.copy_dist()
        (directory / "unexpected.txt").write_text("extra", encoding="utf-8")
        self.assertEqual(self.verify(directory), 1)

    def test_a_tampered_bundle_fails(self) -> None:
        directory = self.copy_dist()
        path = next(directory.glob("code-intel-configs-*.tar.gz"))
        path.write_bytes(path.read_bytes()[:-40] + b"\0" * 40)
        self.assertEqual(self.verify(directory), 1)

    def test_a_tampered_manifest_digest_fails(self) -> None:
        directory = self.copy_dist()
        path = next(directory.glob("code-intel-configs-*.manifest.json"))
        document = json.loads(path.read_text(encoding="utf-8"))
        document["codeIntel"]["minVersion"] = "0.7.1"
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(self.verify(directory), 1)

    def test_a_wrong_tag_fails(self) -> None:
        self.assertEqual(
            verifier.main(["--tag", "configs-v9.9.9", "--dist", str(self.dist), "--no-source"]), 1
        )

    def test_the_source_comparison_rejects_a_rewritten_file(self) -> None:
        commit = git("rev-parse", "HEAD")
        prefix = f"code-intel-pipeline-{commit}/"
        archive = subprocess.run(
            ["git", "archive", "--format=tar", f"--prefix={prefix}", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as stream:
            stream.write(archive)
        files = [dict(entry) for entry in self.manifest["bundle"]["files"]]
        self.assertEqual(verifier.compare_with_source(buffer.getvalue(), prefix, files), len(files))
        files[0]["sha256"] = "0" * 64
        with self.assertRaises(verifier.VerificationError):
            verifier.compare_with_source(buffer.getvalue(), prefix, files)

    def test_the_workflow_ships_the_surface_it_packages(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        for surface in ci.CONFIGS_SURFACE:
            self.assertIn(surface, text, surface)
            self.assertTrue((ROOT / surface).exists(), surface)

    def test_the_workflow_runs_the_gate_on_a_pull_request(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertIn("if: ${{ needs.configs-gate.outputs.publish == 'true' }}", text)
        self.assertNotIn("--clobber", text)


class CompatibilityGateTests(unittest.TestCase):
    """The gate is SemVer precedence, not a lexical or numeric-prefix compare."""

    def block(self) -> dict:
        return ci.configs_document()["codeIntel"]

    def verdict(self, candidate: str) -> bool:
        return verifier.compatibility_verdict(candidate, self.block())[0]

    def test_the_published_binaries_are_refused(self) -> None:
        for candidate in ("0.7.1", "0.7.0", "0.7.2-beta.6"):
            self.assertFalse(self.verdict(candidate), candidate)

    def test_the_declared_floor_is_accepted(self) -> None:
        self.assertTrue(self.verdict("0.7.2-beta.7"))
        self.assertTrue(self.verdict("0.7.2"))
        self.assertTrue(self.verdict("0.7.3"))

    def test_the_range_excludes_the_next_minor(self) -> None:
        self.assertFalse(self.verdict("0.8.0"))
        self.assertTrue(self.verdict("0.8.0-alpha.1"))

    def test_prerelease_identifiers_compare_numerically(self) -> None:
        self.assertLess(verifier.compare_versions("0.7.2-beta.7", "0.7.2-beta.10"), 0)
        self.assertGreater(verifier.compare_versions("0.7.2-beta.10", "0.7.2-beta.7"), 0)
        self.assertLess(verifier.compare_versions("0.7.2-alpha", "0.7.2-beta"), 0)
        self.assertGreater(verifier.compare_versions("0.7.2", "0.7.2-rc.1"), 0)

    @unittest.skipIf(sys.platform == "win32", "a shebang script is not executable on Windows")
    def test_a_binary_outside_the_range_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "code-intel"
            fake.write_text("#!/bin/sh\necho 'code-intel 0.7.1'\n", encoding="utf-8")
            fake.chmod(0o755)
            found = verifier.binary_version(str(fake))
            satisfies, reason = verifier.compatibility_verdict(found, self.block())
            self.assertFalse(satisfies)
            self.assertIn("0.7.1", reason)

    @unittest.skipIf(sys.platform == "win32", "a shebang script is not executable on Windows")
    def test_an_unparseable_version_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "code-intel"
            fake.write_text("#!/bin/sh\necho 'not a version'\n", encoding="utf-8")
            fake.chmod(0o755)
            with self.assertRaises(verifier.VerificationError):
                verifier.binary_version(str(fake))

    def test_binary_version_reads_a_multi_digit_version(self) -> None:
        # Regression: an alternation that tried the bare `0` branch first read
        # "code-intel 0.7.2-beta.6" as "0" and crashed the gate with a traceback
        # instead of refusing the binary.
        found = verifier.binary_version(sys.executable)
        self.assertRegex(found, r"^\d+\.\d+\.\d+")

    def test_the_tag_pattern_is_strict(self) -> None:
        self.assertEqual(
            verifier.expected_asset_names("configs-v0.1.0")[0], "code-intel-configs-0.1.0.tar.gz"
        )
        for bad in ("v0.1.0", "configs-v0.1", "configs-v01.1.0"):
            with self.assertRaises(verifier.VerificationError):
                verifier.expected_asset_names(bad)


class AcceptancePathTests(unittest.TestCase):
    """The acceptance must resolve the binary before it runs anything elsewhere."""

    def test_a_relative_binary_becomes_absolute(self) -> None:
        # Regression: CI passed `--binary target/release/code-intel` while the
        # acceptance runs its subprocesses with cwd outside the checkout, so the
        # relative path resolved to nothing and the job died with FileNotFoundError.
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "code-intel"
            fake.write_text("x", encoding="utf-8")
            resolved = ci.configs_binary(str(fake))
            self.assertTrue(os.path.isabs(resolved))
            self.assertEqual(Path(resolved), fake.resolve())

    def test_a_missing_binary_names_the_resolved_path(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            ci.configs_binary("target/release/does-not-exist")
        self.assertIn("code-intel binary missing", str(caught.exception))

    def test_the_acceptance_stages_the_binary_outside_the_checkout(self) -> None:
        # Configuration discovery walks up from the executable, so a binary left
        # in the build checkout resolves the checkout's own manifest and the
        # bundle under acceptance is never the configuration that gets read.
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            (checkout / "orchestration").mkdir(parents=True)
            (checkout / "orchestration" / "integrations.json").write_text(
                "{}", encoding="utf-8"
            )
            (checkout / "target" / "release").mkdir(parents=True)
            built = checkout / "target" / "release" / "code-intel"
            built.write_text("binary", encoding="utf-8")
            work = Path(temporary) / "work"
            work.mkdir()
            staged = Path(ci.stage_executable(ci.configs_binary(str(built)), work / "bin"))
            self.assertTrue(staged.is_file())
            self.assertEqual(staged.parent, (work / "bin").resolve())
            self.assertEqual(staged.read_text(encoding="utf-8"), "binary")

    def test_the_consumer_root_ships_source_without_the_archive_configuration(self) -> None:
        # The doctor capability validates integration entrypoints under the
        # manifest's root, so a configuration package alone can never complete a
        # normal run; the consumer root must carry the source and the bundle must
        # be the only configuration on it.
        with tempfile.TemporaryDirectory() as temporary:
            root = ci.consumer_root(Path(temporary))
            self.assertTrue((root / "crates").is_dir())
            self.assertFalse((root / "orchestration").exists())


if __name__ == "__main__":
    unittest.main()