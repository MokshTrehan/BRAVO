#!/usr/bin/python3
"""Contract tests for the fixed TurnSafe KAIST-VIO baseline configuration."""

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

import numpy as np
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = REPOSITORY_ROOT / "config" / "kaist_vio_turnsafe_baseline"
ESTIMATOR_CONFIG = CONFIG_ROOT / "estimator_config.yaml"
IMU_CONFIG = CONFIG_ROOT / "kalibr_imu_chain.yaml"
CAMERA_CONFIG = CONFIG_ROOT / "kalibr_imucam_chain.yaml"
LAUNCH_CONFIG = REPOSITORY_ROOT / "project" / "kaist_vio_serial.launch"
ADAPTER_CONTRACT = REPOSITORY_ROOT / "docs" / "turnsafe" / "kaist_vio_adapter_contract.md"

CAM0_TOPIC = "/turnsafe/kaist/infra1/image_raw"
CAM1_TOPIC = "/turnsafe/kaist/infra2/image_raw"
IMU_TOPIC = "/mavros/imu/data"

CAM0_INTRINSICS = [
    380.9229090195708,
    380.29264802262736,
    324.68121181846755,
    224.6741321466431,
]
CAM1_INTRINSICS = [
    380.95187095303424,
    380.3065956074995,
    324.0678433553536,
    225.9586983198407,
]
CAM0_RADTAN = [
    0.006896928127777268,
    -0.009144207062654397,
    0.000254113977103925,
    0.0021434982252719545,
]
CAM1_RADTAN = [
    0.007044055287844759,
    -0.010251485722185347,
    0.0006674304399871926,
    0.001678899816379666,
]

T_CAM0_IMU = np.array(
    [
        [
            -0.04030123999740945,
            -0.9989998755524683,
            0.01936643232049068,
            0.02103955032447366,
        ],
        [
            0.026311325355146964,
            -0.020436499663524704,
            -0.9994448777394171,
            -0.038224929976612206,
        ],
        [
            0.9988410905708309,
            -0.0397693113802049,
            0.027108627033059024,
            -0.1363488241088845,
        ],
        [0.0, 0.0, 0.0, 1.0],
    ],
    dtype=np.float64,
)
T_CAM1_IMU = np.array(
    [
        [
            -0.03905752472566068,
            -0.9990498568899562,
            0.019336318430946575,
            -0.02909273113160158,
        ],
        [
            0.025035478432625047,
            -0.020323396666370924,
            -0.9994799569614147,
            -0.03811090793611019,
        ],
        [
            0.99892328763622,
            -0.03855311914877835,
            0.02580547271309183,
            -0.13656684822705098,
        ],
        [0.0, 0.0, 0.0, 1.0],
    ],
    dtype=np.float64,
)
T_CAM0_TO_CAM1 = np.array(
    [
        [
            0.9999992248836708,
            6.384241340452582e-05,
            0.0012434452955667624,
            -0.049960282472300055,
        ],
        [
            -6.225102643531651e-05,
            0.9999991790958949,
            -0.0012798173093508036,
            -5.920119010064575e-05,
        ],
        [
            -0.001243525981443161,
            0.0012797389115975439,
            0.9999984079544582,
            -0.00014316003395349448,
        ],
        [0.0, 0.0, 0.0, 1.0],
    ],
    dtype=np.float64,
)

CAM0_TIME_SHIFT = -0.029958533056650416
CAM1_TIME_SHIFT = -0.030340187355085417
CAM1_MINUS_CAM0 = -0.000381654298435001
STEREO_BASELINE_METRES = 0.049960522658277336


def load_opencv_yaml(path: Path):
    """Load the repository's `%YAML:1.0` files without changing their bytes."""

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if lines and lines[0].startswith("%YAML:"):
        text = "\n".join(lines[1:]) + ("\n" if text.endswith("\n") else "")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise AssertionError(f"unable to parse required YAML {path}: {error}") from error
    if not isinstance(value, dict):
        raise AssertionError(f"required YAML is not a mapping: {path}")
    return value


def exact_param_map(node: ET.Element):
    result = {}
    for parameter in node.findall("param"):
        name = parameter.get("name")
        if not name:
            raise AssertionError("launch contains a param without a name")
        if name in result:
            raise AssertionError(f"launch contains duplicate param {name!r}")
        result[name] = parameter
    return result


class KaistVioConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        required = [
            ESTIMATOR_CONFIG,
            IMU_CONFIG,
            CAMERA_CONFIG,
            LAUNCH_CONFIG,
            ADAPTER_CONTRACT,
        ]
        missing = [str(path.relative_to(REPOSITORY_ROOT)) for path in required if not path.is_file()]
        if missing:
            raise AssertionError(
                "required TurnSafe KAIST-VIO configuration file(s) absent: "
                + ", ".join(missing)
            )

        cls.estimator = load_opencv_yaml(ESTIMATOR_CONFIG)
        cls.imu = load_opencv_yaml(IMU_CONFIG)
        cls.cameras = load_opencv_yaml(CAMERA_CONFIG)
        cls.contract_text = ADAPTER_CONTRACT.read_text(encoding="utf-8")
        try:
            cls.launch_root = ET.parse(LAUNCH_CONFIG).getroot()
        except ET.ParseError as error:
            raise AssertionError(f"unable to parse required launch XML {LAUNCH_CONFIG}: {error}") from error

    def test_frozen_estimator_mode_and_calibration_flags(self) -> None:
        expected = {
            "use_fej": True,
            "use_stereo": True,
            "max_cameras": 2,
            "feat_rep_msckf": "GLOBAL_3D",
            "up_msckf_landmark_elimination": "schur",
            "up_msckf_max_visual_passes": 1,
            "calib_cam_extrinsics": False,
            "calib_cam_intrinsics": False,
            "calib_cam_timeoffset": False,
            "calib_imu_intrinsics": False,
            "calib_imu_g_sensitivity": False,
            "downsample_cameras": False,
            "num_opencv_threads": 0,
            "multi_threading_pubs": False,
            "multi_threading_subs": False,
        }
        for key, value in expected.items():
            self.assertIn(key, self.estimator, f"missing frozen estimator field {key}")
            self.assertEqual(self.estimator[key], value, f"wrong frozen estimator field {key}")

        self.assertEqual(self.estimator["relative_config_imu"], IMU_CONFIG.name)
        self.assertEqual(self.estimator["relative_config_imucam"], CAMERA_CONFIG.name)

    def test_exact_camera_topics_intrinsics_and_radtan_order(self) -> None:
        for camera_name, topic, intrinsics, radtan in [
            ("cam0", CAM0_TOPIC, CAM0_INTRINSICS, CAM0_RADTAN),
            ("cam1", CAM1_TOPIC, CAM1_INTRINSICS, CAM1_RADTAN),
        ]:
            self.assertIn(camera_name, self.cameras)
            camera = self.cameras[camera_name]
            self.assertEqual(camera["rostopic"], topic)
            self.assertEqual(camera["camera_model"], "pinhole")
            self.assertEqual(camera["distortion_model"], "radtan")
            self.assertEqual(camera["resolution"], [640, 480])
            self.assertEqual(camera["intrinsics"], intrinsics)
            self.assertEqual(camera["distortion_coeffs"], radtan)
            self.assertEqual(
                camera["intrinsics"] + camera["distortion_coeffs"],
                intrinsics + radtan,
                f"{camera_name} must load as [fx,fy,cx,cy,k1,k2,p1,p2]",
            )

        self.assertEqual(self.cameras["cam0"]["cam_overlaps"], [1])
        self.assertEqual(self.cameras["cam1"]["cam_overlaps"], [0])

    def test_exact_extrinsics_stereo_direction_and_baseline(self) -> None:
        configured_cam0 = np.asarray(self.cameras["cam0"]["T_cam_imu"], dtype=np.float64)
        configured_cam1 = np.asarray(self.cameras["cam1"]["T_cam_imu"], dtype=np.float64)
        configured_stereo = np.asarray(self.cameras["cam1"]["T_cn_cnm1"], dtype=np.float64)

        np.testing.assert_array_equal(configured_cam0, T_CAM0_IMU)
        np.testing.assert_array_equal(configured_cam1, T_CAM1_IMU)
        np.testing.assert_array_equal(configured_stereo, T_CAM0_TO_CAM1)

        # Official T_cam_imu maps IMU -> camera. Therefore the transform from
        # cam0 to cam1 is T_cam1_imu * inverse(T_cam0_imu), not the reverse.
        composed_forward = configured_cam1 @ np.linalg.inv(configured_cam0)
        composed_reverse = configured_cam0 @ np.linalg.inv(configured_cam1)
        np.testing.assert_allclose(
            composed_forward,
            configured_stereo,
            rtol=0.0,
            atol=1e-12,
            err_msg="T_cn_cnm1 has the wrong cam0-to-cam1 direction",
        )
        self.assertFalse(
            np.allclose(composed_reverse, configured_stereo, rtol=0.0, atol=1e-3),
            "reverse stereo composition unexpectedly matches T_cn_cnm1",
        )

        rotation = configured_stereo[:3, :3]
        np.testing.assert_allclose(rotation.T @ rotation, np.eye(3), rtol=0.0, atol=1e-9)
        self.assertAlmostEqual(float(np.linalg.det(rotation)), 1.0, places=9)
        baseline = float(np.linalg.norm(configured_stereo[:3, 3]))
        self.assertAlmostEqual(baseline, STEREO_BASELINE_METRES, places=15)
        self.assertLess(configured_stereo[0, 3], 0.0, "cam0-to-cam1 baseline direction changed")

    def test_time_shift_sign_and_declared_cam1_disparity(self) -> None:
        configured_cam0 = self.cameras["cam0"]["timeshift_cam_imu"]
        configured_cam1 = self.cameras["cam1"]["timeshift_cam_imu"]
        self.assertEqual(configured_cam0, CAM0_TIME_SHIFT)
        self.assertEqual(configured_cam1, CAM1_TIME_SHIFT)

        # Repository convention: t_imu = t_cam + dt_CAMtoIMU.
        camera_time = 10.0
        self.assertAlmostEqual(
            camera_time + configured_cam0,
            9.97004146694335,
            places=15,
        )
        disparity = configured_cam1 - configured_cam0
        self.assertAlmostEqual(disparity, CAM1_MINUS_CAM0, places=18)
        self.assertAlmostEqual(abs(disparity) * 1e6, 381.654298435001, places=12)

        self.assertIn("t_imu = t_cam + dt_CAMtoIMU", self.contract_text)
        self.assertIn(
            "dt_cam1 - dt_cam0 = -0.000381654298435001 s",
            self.contract_text,
        )
        self.assertIn("dt_CAMtoIMU = -0.029958533056650416 s", self.contract_text)

    def test_exact_imu_noise_rate_identity_maps_and_gravity(self) -> None:
        self.assertIn("imu0", self.imu)
        imu = self.imu["imu0"]
        expected_scalars = {
            "accelerometer_noise_density": 0.00333388,
            "accelerometer_random_walk": 0.00047402,
            "gyroscope_noise_density": 0.00005770,
            "gyroscope_random_walk": 0.00001565,
            "update_rate": 100.0,
        }
        for key, value in expected_scalars.items():
            self.assertIn(key, imu, f"missing official IMU field {key}")
            self.assertEqual(imu[key], value, f"wrong official IMU field {key}")

        self.assertEqual(imu["rostopic"], IMU_TOPIC)
        self.assertEqual(imu["model"], "kalibr")
        for key in ("Tw", "R_IMUtoGYRO", "Ta", "R_IMUtoACC"):
            np.testing.assert_array_equal(np.asarray(imu[key], dtype=float), np.eye(3))
        np.testing.assert_array_equal(np.asarray(imu["Tg"], dtype=float), np.zeros((3, 3)))
        self.assertEqual(self.estimator["gravity_mag"], 9.805)

    def test_launch_mirrors_frozen_mode_outputs_and_no_ground_truth(self) -> None:
        self.assertEqual(self.launch_root.tag, "launch")
        nodes = self.launch_root.findall(".//node")
        self.assertEqual(len(nodes), 1, "KAIST launch must contain exactly one estimator node")
        node = nodes[0]
        self.assertEqual(node.get("pkg"), "ov_msckf")
        self.assertEqual(node.get("type"), "ros1_serial_msckf")
        self.assertEqual(node.get("required"), "true")
        params = exact_param_map(node)

        expected_params = {
            "bag_start": ("double", "$(arg bag_start)"),
            "bag_durr": ("double", "-1.0"),
            "use_fej": ("bool", "true"),
            "use_stereo": ("bool", "true"),
            "max_cameras": ("int", "2"),
            "cam0_distortion_model": ("str", "radtan"),
            "cam1_distortion_model": ("str", "radtan"),
            "feat_rep_msckf": ("str", "GLOBAL_3D"),
            "up_msckf_landmark_elimination": ("str", "schur"),
            "up_msckf_max_visual_passes": ("int", "1"),
            "calib_cam_extrinsics": ("bool", "false"),
            "calib_cam_intrinsics": ("bool", "false"),
            "calib_cam_timeoffset": ("bool", "false"),
            "calib_imu_intrinsics": ("bool", "false"),
            "calib_imu_g_sensitivity": ("bool", "false"),
            "num_opencv_threads": ("int", "0"),
            "multi_threading_pubs": ("bool", "false"),
            "multi_threading_subs": ("bool", "false"),
            "cam0_rostopic": ("str", CAM0_TOPIC),
            "cam1_rostopic": ("str", CAM1_TOPIC),
            "imu0_rostopic": ("str", IMU_TOPIC),
            "save_total_state": ("bool", "true"),
            "filepath_est": ("str", "$(arg path_state)"),
            "filepath_std": ("str", "$(arg path_std)"),
            "record_timing_information": ("bool", "true"),
            "record_timing_filepath": ("str", "$(arg path_time)"),
        }
        for name, (expected_type, expected_value) in expected_params.items():
            self.assertIn(name, params, f"launch is missing required param {name}")
            self.assertEqual(params[name].get("type"), expected_type, f"wrong type for {name}")
            self.assertEqual(params[name].get("value"), expected_value, f"wrong value for {name}")

        self.assertEqual(params["path_bag"].get("value"), "$(arg bag)")
        self.assertEqual(
            params["config_path"].get("value"),
            "$(find ov_msckf)/../config/kaist_vio_turnsafe_baseline/estimator_config.yaml",
        )

        arguments = {}
        for argument in self.launch_root.findall("arg"):
            name = argument.get("name")
            self.assertIsNotNone(name, "launch contains an arg without a name")
            self.assertNotIn(name, arguments, f"launch contains duplicate arg {name!r}")
            arguments[name] = argument
        for required_output in ("bag", "path_state", "path_std", "path_time"):
            self.assertIn(required_output, arguments)
            self.assertIsNone(
                arguments[required_output].get("default"),
                f"required launch arg {required_output} must not have a default",
            )
        self.assertEqual(arguments["bag_start"].get("default"), "0.0")

        forbidden_names = {"path_gt", "filepath_gt", "dolivetraj"}
        self.assertTrue(forbidden_names.isdisjoint(arguments))
        self.assertTrue(forbidden_names.isdisjoint(params))
        for element in self.launch_root.iter():
            for attribute, value in element.attrib.items():
                lowered = value.lower()
                for forbidden in (
                    "path_gt",
                    "filepath_gt",
                    "dolivetraj",
                    "groundtruth",
                    "/pose_transformed",
                ):
                    self.assertNotIn(
                        forbidden,
                        lowered,
                        f"ground-truth launch attribute {attribute}",
                    )


if __name__ == "__main__":
    unittest.main()
