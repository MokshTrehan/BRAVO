#!/usr/bin/python3
"""Focused tests for independent TurnSafe source/build provenance."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
import finalize_build_manifest  # noqa: E402
import generate_build_provenance  # noqa: E402
import kaist_vio_campaign  # noqa: E402
import source_snapshot  # noqa: E402


class SourceProvenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        files = {
            ".gitignore": "artifacts/\nbuild/\n",
            "ov_core/CMakeLists.txt": "add_library(core src/core.cpp)\n",
            "ov_core/src/core.cpp": "int core() { return 1; }\n",
            "ov_msckf/CMakeLists.txt": "add_library(msckf src/msckf.cpp)\n",
            "ov_msckf/src/msckf.cpp": "int msckf() { return 2; }\n",
            "docs/turnsafe/t0_schema.md": "schema fixture\n",
            "scripts/turnsafe/source_snapshot.py": "snapshot fixture\n",
            "scripts/turnsafe/generate_build_provenance.py": "generate fixture\n",
            "scripts/turnsafe/finalize_build_manifest.py": "finalize fixture\n",
            "scripts/turnsafe/kaist_vio_campaign.py": "campaign fixture\n",
            "scripts/turnsafe/test_kaist_vio_campaign.py": "campaign test fixture\n",
            "scripts/turnsafe/event_ready_association.py": "association fixture\n",
            "scripts/turnsafe/test_event_ready_association.py": "association test fixture\n",
            "scripts/turnsafe/event_ready_corpus.py": "corpus fixture\n",
            "scripts/turnsafe/test_event_ready_corpus.py": "corpus test fixture\n",
            "scripts/turnsafe/baseline_digest.py": "digest fixture\n",
            "scripts/turnsafe/test_baseline_digest.py": "digest test fixture\n",
            "scripts/turnsafe/test_source_provenance.py": "test fixture\n",
        }
        for relative, payload in files.items():
            path = self.repository / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        self._git("init", "-q")
        self._git("add", ".")
        self._git(
            "-c",
            "user.name=TurnSafe Test",
            "-c",
            "user.email=turnsafe@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(self, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(self.repository)] + list(arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _configure(self):
        output = self.root / "workspace"
        binary_directory = output / "build" / "ov_msckf"
        generated = binary_directory / "turnsafe-generated"
        generated.mkdir(parents=True)
        snapshot_path = generated / "source_snapshot.json"
        source_snapshot.write_snapshot(self.repository, snapshot_path)
        arguments = argparse.Namespace(
            source_snapshot=str(snapshot_path),
            schema=str(self.repository / "docs/turnsafe/t0_schema.md"),
            output_header=str(generated / "TurnSafeBuildProvenance.generated.h"),
            output_manifest=str(generated / "configure_provenance.json"),
            build_type="Release",
            cxx_compiler="/usr/bin/c++",
            cxx_compiler_id="GNU",
            cxx_compiler_version="9.4.0",
            cmake_generator="Unix Makefiles",
            cmake_version="3.16.3",
            cmake_cxx_flags_cache="",
            cmake_cxx_flags_effective="-O3",
            ceres_dir="/read-only/Ceres",
            enable_ros="ON",
            catkin_enable_testing="ON",
            cmake_source_directory=str(self.repository / "ov_msckf"),
            cmake_binary_directory=str(binary_directory),
        )
        configure = generate_build_provenance.generate(arguments)
        return output, snapshot_path, configure

    def _build_manifest(self):
        output, snapshot_path, configure = self._configure()
        binary_directory = output / "build" / "ov_msckf"
        generated = binary_directory / "turnsafe-generated"
        binary = output / "devel/lib/ov_msckf/ros1_serial_msckf"
        msckf = output / "devel/lib/libov_msckf_lib.so"
        core = output / "devel/lib/libov_core_lib.so"
        init = output / "devel/lib/libov_init_lib.so"
        cache = binary_directory / "CMakeCache.txt"
        flags = binary_directory / "CMakeFiles/ov_msckf_lib.dir/flags.make"
        for path, payload in (
            (binary, b"binary fixture"),
            (msckf, b"msckf fixture"),
            (core, b"core fixture"),
            (init, b"init fixture"),
            (
                cache,
                (
                    "CATKIN_ENABLE_TESTING:BOOL=ON\n"
                    "CMAKE_BUILD_TYPE:STRING=Release\n"
                    "CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/c++\n"
                    "CMAKE_CXX_FLAGS:STRING=\n"
                    "Ceres_DIR:PATH=/read-only/Ceres\n"
                    "ENABLE_ROS:BOOL=ON\n"
                    "ov_msckf_BINARY_DIR:STATIC={}\n"
                    "CMAKE_GENERATOR:INTERNAL=Unix Makefiles\n"
                    "CMAKE_HOME_DIRECTORY:INTERNAL={}\n"
                ).format(binary_directory, self.repository / "ov_msckf").encode(
                    "utf-8"
                ),
            ),
            (flags, b"CXX_FLAGS = -O3 -DNDEBUG -fPIC -std=c++14\n"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        binary.chmod(0o755)
        manifest_path = output / "build_manifest.json"
        manifest = finalize_build_manifest.finalize(
            argparse.Namespace(
                configure_manifest=str(generated / "configure_provenance.json"),
                cmake_cache=str(cache),
                schema=str(self.repository / "docs/turnsafe/t0_schema.md"),
                output=str(manifest_path),
                artifact=[
                    ("estimator_binary", binary),
                    ("ov_msckf_library", msckf),
                    ("ov_core_library", core),
                    ("ov_init_library", init),
                ],
            )
        )
        return output, binary, manifest_path, manifest

    def test_clean_snapshot_is_canonical_and_ignored_evidence_is_absent(self) -> None:
        first = source_snapshot.create_snapshot(self.repository)
        self.assertFalse(first["source_dirty"])
        self.assertEqual(first["untracked_compiled_inputs"], [])
        self.assertEqual(first["untracked_provenance_inputs"], [])
        evidence = self.repository / "artifacts" / "turnsafe" / "result.json"
        evidence.parent.mkdir(parents=True)
        evidence.write_text("{}\n", encoding="ascii")
        second = source_snapshot.create_snapshot(self.repository)
        self.assertEqual(
            first["aggregate_source_snapshot_sha256"],
            second["aggregate_source_snapshot_sha256"],
        )

    def test_tracked_staged_and_untracked_compiled_changes_are_bound(self) -> None:
        clean = source_snapshot.create_snapshot(self.repository)
        tracked = self.repository / "ov_core" / "src" / "core.cpp"
        tracked.write_text("int core() { return 3; }\n", encoding="utf-8")
        self._git("add", str(tracked.relative_to(self.repository)))
        staged = source_snapshot.create_snapshot(self.repository)
        self.assertTrue(staged["source_dirty"])
        self.assertNotEqual(
            clean["aggregate_source_snapshot_sha256"],
            staged["aggregate_source_snapshot_sha256"],
        )
        untracked = self.repository / "ov_msckf" / "test" / "new_test.cpp"
        untracked.parent.mkdir(parents=True)
        untracked.write_text("int test_fixture;\n", encoding="utf-8")
        dirty = source_snapshot.create_snapshot(self.repository)
        self.assertEqual(
            [item["path"] for item in dirty["untracked_compiled_inputs"]],
            ["ov_msckf/test/new_test.cpp"],
        )
        self.assertNotEqual(
            staged["aggregate_source_snapshot_sha256"],
            dirty["aggregate_source_snapshot_sha256"],
        )

    def test_untracked_curated_python_is_byte_bound_and_marks_source_dirty(self) -> None:
        clean = source_snapshot.create_snapshot(self.repository)
        relative = "scripts/turnsafe/event_ready_association.py"
        self._git("rm", "--cached", relative)
        path = self.repository / relative
        path.write_text("association changed while untracked\n", encoding="utf-8")
        dirty = source_snapshot.create_snapshot(self.repository)
        self.assertTrue(dirty["source_dirty"])
        self.assertEqual(
            [item["path"] for item in dirty["untracked_provenance_inputs"]],
            [relative],
        )
        self.assertNotEqual(
            clean["aggregate_source_snapshot_sha256"],
            dirty["aggregate_source_snapshot_sha256"],
        )

    def test_configure_provenance_rejects_snapshot_aggregate_tamper(self) -> None:
        output, snapshot_path, _ = self._configure()
        value = json.loads(snapshot_path.read_text(encoding="ascii"))
        value["head_sha"] = "0" * 40
        snapshot_path.write_text(json.dumps(value), encoding="ascii")
        with self.assertRaisesRegex(
            generate_build_provenance.ProvenanceError, "aggregate mismatch"
        ):
            generate_build_provenance.generate(
                argparse.Namespace(
                    source_snapshot=str(snapshot_path),
                    schema=str(self.repository / "docs/turnsafe/t0_schema.md"),
                    output_header=str(output / "bad.h"),
                    output_manifest=str(output / "bad.json"),
                    build_type="Release",
                    cxx_compiler="/usr/bin/c++",
                    cxx_compiler_id="GNU",
                    cxx_compiler_version="9.4.0",
                    cmake_generator="Unix Makefiles",
                    cmake_version="3.16.3",
                    cmake_cxx_flags_cache="",
                    cmake_cxx_flags_effective="-O3",
                    ceres_dir="/read-only/Ceres",
                    enable_ros="ON",
                    catkin_enable_testing="ON",
                    cmake_source_directory=str(self.repository / "ov_msckf"),
                    cmake_binary_directory=str(output / "build" / "ov_msckf"),
                )
            )

    def test_configure_binds_base_and_event_extension_schema_identifiers(self) -> None:
        output, _, configure = self._configure()
        self.assertEqual(
            configure["descriptor"]["diagnostic_schema_identifiers"],
            {
                "base": "turnsafe.t0.v1",
                "event_extension": "turnsafe.t0.event_extension.v1",
            },
        )
        header = (
            output / "build" / "ov_msckf" / "turnsafe-generated" /
            "TurnSafeBuildProvenance.generated.h"
        ).read_text(encoding="ascii")
        self.assertIn("kDiagnosticBaseSchema", header)
        self.assertIn("kDiagnosticEventExtensionSchema", header)

    def test_parser_accepts_leading_dash_and_empty_cxx_flags(self) -> None:
        parser = generate_build_provenance._parser()
        common = [
            "--source-snapshot", "snapshot.json",
            "--schema", "schema.md",
            "--output-header", "generated.h",
            "--output-manifest", "configure.json",
            "--build-type", "Release",
            "--cxx-compiler", "/usr/bin/c++",
            "--cxx-compiler-id", "GNU",
            "--cxx-compiler-version", "9.4.0",
            "--cmake-generator", "Unix Makefiles",
            "--cmake-version", "3.16.3",
            "--ceres-dir", "/read-only/Ceres",
            "--enable-ros", "ON",
            "--catkin-enable-testing", "ON",
            "--cmake-source-directory", "/source",
            "--cmake-binary-directory", "/build",
        ]
        for value in ("-O3 -g", ""):
            parsed = parser.parse_args(
                common
                + [
                    "--cmake-cxx-flags-cache={}".format(value),
                    "--cmake-cxx-flags-effective={}".format(value),
                ]
            )
            self.assertEqual(parsed.cmake_cxx_flags_cache, value)
            self.assertEqual(parsed.cmake_cxx_flags_effective, value)

    def test_wrong_initial_or_mutated_cache_fails_closed(self) -> None:
        output, binary, manifest_path, _ = self._build_manifest()
        cache = output / "build/ov_msckf/CMakeCache.txt"
        cache.write_text(
            cache.read_text(encoding="utf-8").replace(
                "CMAKE_BUILD_TYPE:STRING=Release",
                "CMAKE_BUILD_TYPE:STRING=Debug",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            finalize_build_manifest.ManifestError, "CMAKE_BUILD_TYPE"
        ):
            finalize_build_manifest.finalize(
                argparse.Namespace(
                    configure_manifest=str(
                        output
                        / "build/ov_msckf/turnsafe-generated/configure_provenance.json"
                    ),
                    cmake_cache=str(cache),
                    schema=str(self.repository / "docs/turnsafe/t0_schema.md"),
                    output=str(output / "bad_build_manifest.json"),
                    artifact=[
                        ("estimator_binary", binary),
                        (
                            "ov_msckf_library",
                            output / "devel/lib/libov_msckf_lib.so",
                        ),
                        (
                            "ov_core_library",
                            output / "devel/lib/libov_core_lib.so",
                        ),
                        (
                            "ov_init_library",
                            output / "devel/lib/libov_init_lib.so",
                        ),
                    ],
                )
            )
        with self.assertRaisesRegex(
            kaist_vio_campaign.CampaignError, "CMake cache"
        ):
            kaist_vio_campaign._validate_build_manifest(
                manifest_path,
                binary.resolve(),
                (self.repository / "docs/turnsafe/t0_schema.md").resolve(),
                self.repository.resolve(),
            )

    def test_cached_and_effective_flags_are_bound_separately(self) -> None:
        output, binary, manifest_path, manifest = self._build_manifest()
        configuration = manifest["descriptor"]["configuration"]
        self.assertEqual(configuration["cmake_cxx_flags_cache"], "")
        self.assertEqual(configuration["cmake_cxx_flags_effective"], "-O3")
        flags = output / "build/ov_msckf/CMakeFiles/ov_msckf_lib.dir/flags.make"
        flags.write_text(
            "CXX_FLAGS = -O2 -DNDEBUG -fPIC -std=c++14\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            kaist_vio_campaign.CampaignError, "target compile flags"
        ):
            kaist_vio_campaign._validate_build_manifest(
                manifest_path,
                binary.resolve(),
                (self.repository / "docs/turnsafe/t0_schema.md").resolve(),
                self.repository.resolve(),
            )

    def test_manifest_embedded_forbidden_path_is_rejected_before_hash(self) -> None:
        _, binary, manifest_path, _ = self._build_manifest()
        value = json.loads(manifest_path.read_text(encoding="ascii"))
        value["artifacts"]["ov_core_library"]["path"] = str(
            self.root / "PRIVATE" / "libov_core_lib.so"
        )
        body = dict(value)
        body.pop("build_manifest_payload_sha256", None)
        value["build_manifest_payload_sha256"] = (
            kaist_vio_campaign._sha256_canonical_json(body)
        )
        manifest_path.write_text(
            json.dumps(value, sort_keys=True, indent=2) + "\n",
            encoding="ascii",
        )
        with self.assertRaisesRegex(
            kaist_vio_campaign.CampaignError, "forbidden"
        ):
            kaist_vio_campaign._validate_build_manifest(
                manifest_path,
                binary.resolve(),
                (self.repository / "docs/turnsafe/t0_schema.md").resolve(),
                self.repository.resolve(),
            )

    def test_caller_source_tree_build_and_state_conflicts_fail(self) -> None:
        source = {
            "head_sha": "1" * 40,
            "head_tree": "2" * 40,
            "source_dirty": True,
        }
        build_id = "3" * 64
        kaist_vio_campaign._validate_t0_caller_expectations(
            source, build_id, "1" * 40, "2" * 40, build_id, "dirty"
        )
        cases = (
            ("0" * 40, "2" * 40, build_id, "dirty", "source SHA"),
            ("1" * 40, "0" * 40, build_id, "dirty", "source tree"),
            ("1" * 40, "2" * 40, "0" * 64, "dirty", "build ID"),
            ("1" * 40, "2" * 40, build_id, "clean", "source state"),
        )
        for sha, tree, expected_build, state, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                kaist_vio_campaign.CampaignError, message
            ):
                kaist_vio_campaign._validate_t0_caller_expectations(
                    source, build_id, sha, tree, expected_build, state
                )

    def test_campaign_recomputes_manifest_artifact_and_schema_identities(self) -> None:
        output, binary, manifest_path, manifest = self._build_manifest()
        binding = kaist_vio_campaign._validate_build_manifest(
            manifest_path,
            binary.resolve(),
            (self.repository / "docs/turnsafe/t0_schema.md").resolve(),
            self.repository.resolve(),
        )
        self.assertEqual(
            binding["build_provenance_id"], manifest["build_provenance_id"]
        )
        binary.write_bytes(b"mutated binary")
        with self.assertRaisesRegex(
            kaist_vio_campaign.CampaignError, "artifact estimator_binary"
        ):
            kaist_vio_campaign._validate_build_manifest(
                manifest_path,
                binary.resolve(),
                (self.repository / "docs/turnsafe/t0_schema.md").resolve(),
                self.repository.resolve(),
            )

    def test_campaign_rejects_well_formed_false_build_id(self) -> None:
        _, binary, manifest_path, _ = self._build_manifest()
        value = json.loads(manifest_path.read_text(encoding="ascii"))
        value["build_provenance_id"] = "0" * 64
        body = dict(value)
        body.pop("build_manifest_payload_sha256", None)
        value["build_manifest_payload_sha256"] = (
            kaist_vio_campaign._sha256_canonical_json(body)
        )
        manifest_path.write_text(
            json.dumps(value, sort_keys=True, indent=2) + "\n",
            encoding="ascii",
        )
        with self.assertRaisesRegex(
            kaist_vio_campaign.CampaignError, "build-provenance ID mismatch"
        ):
            kaist_vio_campaign._validate_build_manifest(
                manifest_path,
                binary.resolve(),
                (self.repository / "docs/turnsafe/t0_schema.md").resolve(),
                self.repository.resolve(),
            )

    def test_schema_mutation_after_build_fails(self) -> None:
        _, binary, manifest_path, _ = self._build_manifest()
        schema = self.repository / "docs/turnsafe/t0_schema.md"
        schema.write_text("mutated schema\n", encoding="utf-8")
        with self.assertRaisesRegex(
            kaist_vio_campaign.CampaignError, "diagnostic schema"
        ):
            kaist_vio_campaign._validate_build_manifest(
                manifest_path,
                binary.resolve(),
                schema.resolve(),
                self.repository.resolve(),
            )


if __name__ == "__main__":
    unittest.main()
