#!/usr/bin/python3
"""Focused invariants for conditioning/Schema-2 replay profiles and launch XML."""

from __future__ import annotations

import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import run_ros1_replay
from verify_replay_profiles import (
    PROFILE_SPECS,
    REPOSITORY_ROOT,
    SCHEMA2_PROFILE_IDS,
    read_scalars,
    sha256,
    validate_profiles,
)


EXPECTED_NULLSPACE_SCHEMA2_HASHES = {
    "euroc-schema2-nullspace": (
        "21b731a6a3a7fbb5effcc8c84069ef20fe239ca77d498dd5333a7ad4d7b0864c"
    ),
    "tumvi-schema2-nullspace": (
        "b57efce160889c2957a742a1633e3e2125d7065a78b7a8b9153e230d7039edbb"
    ),
}


class ReplayProfileTests(unittest.TestCase):
    def test_nullspace_schema2_profiles_are_declared_reference_profiles(self) -> None:
        report = validate_profiles()
        self.assertEqual(set(report), set(PROFILE_SPECS))
        self.assertEqual(run_ros1_replay.SCHEMA2_PROFILE_IDS, SCHEMA2_PROFILE_IDS)
        for profile_id, expected_hash in EXPECTED_NULLSPACE_SCHEMA2_HASHES.items():
            with self.subTest(profile_id=profile_id):
                path, method, model = PROFILE_SPECS[profile_id]
                self.assertIn(profile_id, SCHEMA2_PROFILE_IDS)
                self.assertEqual(method, "nullspace")
                self.assertEqual(
                    model,
                    "radtan" if profile_id.startswith("euroc-") else "equidistant",
                )
                self.assertEqual(report[profile_id]["method"], "nullspace")
                self.assertEqual(report[profile_id]["camera_model"], model)
                self.assertEqual(report[profile_id]["sha256"], expected_hash)
                self.assertEqual(sha256(path), expected_hash)
                self.assertRegex(expected_hash, re.compile(r"[0-9a-f]{64}\Z"))

    def test_nullspace_schema2_profiles_only_add_default_off_selector(self) -> None:
        pairs = (
            ("euroc-nullspace", "euroc-schema2-nullspace"),
            ("tumvi-nullspace", "tumvi-schema2-nullspace"),
        )
        private_keys = (
            "up_msckf_update_envelope_capture_path",
            "up_msckf_update_envelope_run_id",
            "up_msckf_update_envelope_sequence_id",
        )
        for base_id, schema2_id in pairs:
            with self.subTest(schema2_id=schema2_id):
                base = read_scalars(PROFILE_SPECS[base_id][0])
                schema2 = read_scalars(PROFILE_SPECS[schema2_id][0])
                self.assertEqual(
                    schema2.pop("up_msckf_capture_update_envelopes_v2"),
                    "false",
                )
                self.assertEqual(schema2, base)
                self.assertTrue(all(key not in schema2 for key in private_keys))

    def test_launch_xml_exposes_only_the_required_schema2_arguments(self) -> None:
        launch_path = Path(run_ros1_replay.LAUNCH_PATH)
        root = ET.parse(launch_path).getroot()
        arguments = {element.attrib["name"] for element in root.findall("arg")}
        expected_arguments = {
            "capture_update_envelopes_v2",
            "update_envelope_capture_path",
            "update_envelope_run_id",
            "update_envelope_sequence_id",
        }
        self.assertTrue(expected_arguments.issubset(arguments))
        argument_elements = {
            element.attrib["name"]: element.attrib for element in root.findall("arg")
        }
        self.assertEqual(
            argument_elements["capture_update_envelopes_v2"].get("default"),
            "false",
        )
        for name in expected_arguments - {"capture_update_envelopes_v2"}:
            self.assertEqual(argument_elements[name].get("default"), "")

        node = root.find("node")
        self.assertIsNotNone(node)
        schema2_parameters = {
            element.attrib["name"]: element.attrib
            for element in node.findall("param")
            if element.attrib.get("name", "").startswith("up_msckf_update_envelope_")
            or element.attrib.get("name") == "up_msckf_capture_update_envelopes_v2"
        }
        self.assertEqual(
            set(schema2_parameters),
            {
                "up_msckf_capture_update_envelopes_v2",
                "up_msckf_update_envelope_capture_path",
                "up_msckf_update_envelope_run_id",
                "up_msckf_update_envelope_sequence_id",
            },
        )
        for attributes in schema2_parameters.values():
            self.assertEqual(
                attributes.get("if"), "$(arg capture_update_envelopes_v2)"
            )
        self.assertEqual(
            schema2_parameters["up_msckf_capture_update_envelopes_v2"]["type"],
            "bool",
        )
        self.assertEqual(
            schema2_parameters["up_msckf_capture_update_envelopes_v2"]["value"],
            "true",
        )
        for name in (
            "up_msckf_update_envelope_capture_path",
            "up_msckf_update_envelope_run_id",
            "up_msckf_update_envelope_sequence_id",
        ):
            self.assertEqual(schema2_parameters[name]["type"], "str")
        self.assertEqual(
            schema2_parameters["up_msckf_update_envelope_capture_path"]["value"],
            "$(arg update_envelope_capture_path)",
        )
        self.assertEqual(
            schema2_parameters["up_msckf_update_envelope_run_id"]["value"],
            "$(arg update_envelope_run_id)",
        )
        self.assertEqual(
            schema2_parameters["up_msckf_update_envelope_sequence_id"]["value"],
            "$(arg update_envelope_sequence_id)",
        )

    def test_selected_nullspace_schema2_profiles_validate_individually(self) -> None:
        for profile_id in EXPECTED_NULLSPACE_SCHEMA2_HASHES:
            with self.subTest(profile_id=profile_id):
                path = PROFILE_SPECS[profile_id][0]
                selected = validate_profiles(path)
                self.assertEqual(tuple(selected), (profile_id,))
                self.assertEqual(Path(selected[profile_id]["path"]), path.resolve())
                Path(selected[profile_id]["path"]).relative_to(REPOSITORY_ROOT)

    def test_runner_accepts_nullspace_schema2_reference_capture_domain(self) -> None:
        with tempfile.TemporaryDirectory(prefix="schema2-profile-test-") as directory:
            root = Path(directory)
            bag = root / "input.bag"
            bag.touch()
            for index, profile_id in enumerate(EXPECTED_NULLSPACE_SCHEMA2_HASHES):
                with self.subTest(profile_id=profile_id):
                    output = root / f"output-{index}"
                    capture = output / "updates.scvio"
                    args = run_ros1_replay.parse_args(
                        (
                            "--workspace-setup",
                            str(root / "setup.bash"),
                            "--bag",
                            str(bag),
                            "--config",
                            str(PROFILE_SPECS[profile_id][0]),
                            "--output-dir",
                            str(output),
                            "--capture-update-envelopes-v2",
                            "--update-envelope-capture-path",
                            str(capture),
                            "--update-envelope-run-id",
                            "reference-run",
                            "--update-envelope-sequence-id",
                            "reference-sequence",
                            "--timeout-seconds",
                            "1",
                            "--ros-master-port",
                            str(12400 + index),
                        )
                    )
                    _bag, config, result_root, schema1, schema2 = (
                        run_ros1_replay.validate_args(args)
                    )
                    self.assertEqual(config, PROFILE_SPECS[profile_id][0].resolve())
                    self.assertEqual(result_root, output.resolve())
                    self.assertIsNone(schema1)
                    self.assertEqual(schema2, capture.resolve())


if __name__ == "__main__":
    unittest.main()
