#!/usr/bin/python3
"""Focused tests for the KAIST runtime-identity validator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "kaist_runtime_identity.py"
SPEC = importlib.util.spec_from_file_location("kaist_runtime_identity", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def _artifact(path: Path) -> dict:
    stat_value = path.stat()
    return {
        "path": str(path.resolve()),
        "size_bytes": stat_value.st_size,
        "mtime_ns": stat_value.st_mtime_ns,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class LddParserTests(unittest.TestCase):
    def test_parses_symlinked_and_space_containing_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = root / "path with space" / "libx.so.1.2"
            library.parent.mkdir()
            library.write_bytes(b"x")
            link = root / "path with space" / "libx.so.1"
            link.symlink_to(library.name)
            loader = root / "ld-linux.so.2"
            loader.write_bytes(b"loader")
            text = (
                "linux-vdso.so.1 (0x00007fff00000000)\n"
                f"libx.so.1 => {link} (0x00007f0000000000)\n"
                f"{loader} (0x00007f0000001000)\n"
            )
            resolved, virtual = runtime._parse_ldd_output(text)
            self.assertEqual(virtual, ["linux-vdso.so.1"])
            self.assertEqual(resolved["libx.so.1"]["loader_path"], str(link))
            self.assertEqual(
                resolved["libx.so.1"]["canonical_path"], str(library.resolve())
            )
            self.assertIn(loader.name, resolved)

    def test_rejects_missing_or_unrecognized_dependencies(self) -> None:
        with self.assertRaises(runtime.RuntimeIdentityError):
            runtime._parse_ldd_output("libx.so => not found\n")
        with self.assertRaises(runtime.RuntimeIdentityError):
            runtime._parse_ldd_output("surprising diagnostic\n")

    def test_rejects_loader_injection_environment(self) -> None:
        with self.assertRaises(runtime.RuntimeIdentityError):
            runtime._resolve_dynamic_libraries(
                Path("/bin/true"), {"LD_PRELOAD": "/tmp/untrusted.so"}
            )


class FilePinTests(unittest.TestCase):
    def test_build_id_is_required_to_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "elf"
            path.write_bytes(b"fixture")
            pin = {
                "loader_path": str(path),
                "canonical_path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "build_id": "a" * 40,
            }
            with mock.patch.object(runtime, "_elf_build_id", return_value="b" * 40):
                with self.assertRaises(runtime.RuntimeIdentityError):
                    runtime._validate_file_pin(
                        pin, runtime._FileInspector(), "fixture executable"
                    )


class ProvenanceTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple:
        executable = root / "estimator"
        core = root / "libov_core_lib.so"
        ceres = root / "libceres.so.1.14.0"
        executable.write_bytes(b"estimator-bytes")
        core.write_bytes(b"core-library")
        ceres.write_bytes(b"ceres-library")
        executable_record = _artifact(executable)
        core_record = _artifact(core)
        ceres_record = _artifact(ceres)
        provenance_value = {
            "schema_version": 1,
            "source_commit": "a" * 40,
            "estimator_executable": executable_record,
            "ceres_library": ceres_record,
            "runtime_artifacts": [core_record, executable_record],
        }
        provenance = root / "CP0_BUILD_PROVENANCE.json"
        provenance.write_text(
            json.dumps(provenance_value, sort_keys=True), encoding="utf-8"
        )
        system_policy = {
            "provenance": {
                "path": str(provenance),
                "size_bytes": provenance.stat().st_size,
                "sha256": hashlib.sha256(provenance.read_bytes()).hexdigest(),
                "schema_version": 1,
                "source_commit": "a" * 40,
                "runtime_dependency_sonames": ["libov_core_lib.so"],
                "ceres_soname": "libceres.so.1",
            }
        }
        return system_policy, executable, core, ceres

    def test_validates_every_declared_runtime_artifact_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system_policy, executable, core, ceres = self._fixture(root)
            inspector = runtime._FileInspector()
            executable_identity = inspector.inspect(executable)
            dependencies = {
                "libov_core_lib.so": inspector.inspect(core),
                "libceres.so.1": inspector.inspect(ceres),
            }
            value = runtime._validate_s1_provenance(
                system_policy, inspector, executable_identity, dependencies
            )
            self.assertEqual(value["runtime_artifact_count"], 2)

            old_stat = core.stat()
            core.write_bytes(b"evil-library")
            self.assertEqual(core.stat().st_size, old_stat.st_size)
            os.utime(core, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            changed_inspector = runtime._FileInspector()
            with self.assertRaises(runtime.RuntimeIdentityError):
                runtime._validate_s1_provenance(
                    system_policy,
                    changed_inspector,
                    changed_inspector.inspect(executable),
                    {
                        "libov_core_lib.so": changed_inspector.inspect(core),
                        "libceres.so.1": changed_inspector.inspect(ceres),
                    },
                )

    def test_rejects_duplicate_json_keys(self) -> None:
        with self.assertRaises(runtime.RuntimeIdentityError):
            json.loads('{"a": 1, "a": 2}', object_pairs_hook=runtime._strict_object)


if __name__ == "__main__":
    unittest.main()
