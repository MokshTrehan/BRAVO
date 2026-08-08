#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical CP2-D sequence-math request/response worker.

This file is intended to be inventoried inside the approved direct-math
capsule and invoked through its absolute launcher.  JSON floating-point values
are forbidden: every numerical input and output is transported as exact
big-endian IEEE-754 binary64 bits.  The worker performs no registry, bag,
ground-truth-path, repository, network, or result-directory discovery.
"""

from __future__ import annotations

import sys

import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
from typing import Any, Mapping, Optional, Sequence, Tuple

import cp2_f64_codec as f64_codec
import cp2_direct_kat as direct_kat
import cp2_fp_control as fp_control
if fp_control.CAPSULE_NATIVE:
    # CPython startup deliberately selects the platform-default x87 precision,
    # so the worker must establish the frozen state before importing NumPy or
    # the numerical implementation.  The static launcher has already checked
    # that the hardware accepts the same controls before exec.
    fp_control.establish()

import numpy as np
try:
    from numpy._core import _multiarray_umath as np_cpu_dispatch
except ModuleNotFoundError:  # Construction-host tests may use pre-2.0 NumPy.
    from numpy.core import _multiarray_umath as np_cpu_dispatch

import cp2_sequence_math_codec as transport
import cp2_sequence_math as sequence_math


SCHEMA_VERSION = transport.SCHEMA_VERSION
REQUEST_RECORD_TYPE = transport.REQUEST_RECORD_TYPE
RESPONSE_RECORD_TYPE = transport.RESPONSE_RECORD_TYPE
MAX_DOCUMENT_BYTES = transport.MAX_DOCUMENT_BYTES
MAX_POSE_COUNT = 1_000_000
MAX_ARRAY_ELEMENTS = 9 * MAX_POSE_COUNT
REQUIRED_MEMFD_SEALS = (
    fcntl.F_SEAL_WRITE
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_SEAL
)
SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
MODES = ("nullspace", "schur")
REQUIRED_THREAD_ENVIRONMENT = {
    "BLIS_NUM_THREADS": "1",
    "MKL_DYNAMIC": "FALSE",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "NPY_DISABLE_CPU_FEATURES": "X86_V3,X86_V4,AVX512_ICL,AVX512_SPR",
    "OMP_DYNAMIC": "FALSE",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_CORETYPE": "SkylakeX",
    "OPENBLAS_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
}
REQUIRED_FIXED_ENVIRONMENT = dict(
    REQUIRED_THREAD_ENVIRONMENT,
    LANG="C",
    LC_ALL="C",
    PYTHONDONTWRITEBYTECODE="1",
    PYTHONNOUSERSITE="1",
    TZ="UTC",
)
PRIVATE_ENVIRONMENT_SUFFIXES = {
    "HOME": "home",
    "MPLCONFIGDIR": "mpl",
    "TMPDIR": "tmp",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_CONFIG_HOME": "xdg-config",
}
FORBIDDEN_INJECTION_ENVIRONMENT = (
    "BASH_ENV", "CDPATH", "CONDA_PREFIX", "ENV", "GCONV_PATH",
    "GLIBC_TUNABLES", "LD_AUDIT", "LD_LIBRARY_PATH", "LD_PRELOAD", "PATH",
    "PYTHONBREAKPOINT", "PYTHONHOME", "PYTHONINSPECT", "PYTHONPATH",
    "PYTHONSTARTUP", "PYTHONWARNINGS", "VIRTUAL_ENV",
)
OPENBLAS_LIBRARY_SHA256 = "05c9f9eb89ee68a4b9d673184fa91c99587e736392c0c2d49180a8aa5303d080"
EXPECTED_OPENBLAS_IDENTITY = {
    "config": (
        "OpenBLAS 0.3.31.188.0  USE64BITINT DYNAMIC_ARCH "
        "NO_AFFINITY SkylakeX MAX_THREADS=64"
    ),
    "corename": "SkylakeX",
    "parallel": 1,
    "threads": 1,
}
EXPECTED_X86_MACHINE = {
    "vendor": "AuthenticAMD",
    "family": 26,
    "model": 68,
    "stepping": 0,
}
EXPECTED_NUMPY_BASELINE = ["X86_V2"]
EXPECTED_NUMPY_DISPATCH = ["X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR"]


class SequenceMathWorkerError(ValueError):
    """Raised when exact transport or authoritative sequence math fails."""


def _fail(message: str) -> None:
    raise SequenceMathWorkerError(message)


def _canonical(value: Mapping[str, Any]) -> bytes:
    try:
        payload = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SequenceMathWorkerError("math document is not canonical JSON data") from exc
    if len(payload) > MAX_DOCUMENT_BYTES:
        _fail("math document exceeds its frozen size bound")
    return payload


def _array(value: Any, shape: Optional[Tuple[int, ...]] = None) -> Mapping[str, Any]:
    array = np.asarray(value, dtype=np.float64)
    if shape is not None and array.shape != shape:
        _fail("internal output shape differs")
    if not np.all(np.isfinite(array)):
        _fail("internal output contains a nonfinite value")
    _checked_element_count(tuple(int(item) for item in array.shape), "internal output")
    return {
        "shape": list(array.shape),
        "bits": [f64_codec.f64_to_bits_hex(float(item)) for item in array.ravel(order="C")],
    }


def _scalar(value: Any) -> str:
    return f64_codec.f64_to_bits_hex(float(value))


def _checked_element_count(shape: Tuple[int, ...], label: str) -> int:
    count = 1
    for dimension in shape:
        if type(dimension) is not int or dimension < 0:
            _fail(label + " has an invalid array dimension")
        if count and dimension > MAX_ARRAY_ELEMENTS // count:
            _fail(label + " array element count exceeds its frozen bound")
        count *= dimension
    if count > MAX_ARRAY_ELEMENTS:
        _fail(label + " array element count exceeds its frozen bound")
    return count


def _rotation_quaternion_xyzw(rotation: Any) -> np.ndarray:
    matrix = np.asarray(rotation, dtype=np.float64)
    sequence_math.validate_proper_rotation(matrix)
    trace = np.float64(matrix[0, 0] + matrix[1, 1] + matrix[2, 2])
    if trace > np.float64(0.0):
        scale = np.float64(math.sqrt(float(np.float64(trace + np.float64(1.0)))) * 2.0)
        w = np.float64(0.25 * scale)
        x = np.float64((matrix[2, 1] - matrix[1, 2]) / scale)
        y = np.float64((matrix[0, 2] - matrix[2, 0]) / scale)
        z = np.float64((matrix[1, 0] - matrix[0, 1]) / scale)
    else:
        diagonal = tuple(np.float64(matrix[index, index]) for index in range(3))
        axis = max(range(3), key=lambda index: float(diagonal[index]))
        if axis == 0:
            scale = np.float64(
                math.sqrt(float(np.float64(1.0 + diagonal[0] - diagonal[1] - diagonal[2])))
                * 2.0
            )
            x = np.float64(0.25 * scale)
            y = np.float64((matrix[0, 1] + matrix[1, 0]) / scale)
            z = np.float64((matrix[0, 2] + matrix[2, 0]) / scale)
            w = np.float64((matrix[2, 1] - matrix[1, 2]) / scale)
        elif axis == 1:
            scale = np.float64(
                math.sqrt(float(np.float64(1.0 + diagonal[1] - diagonal[0] - diagonal[2])))
                * 2.0
            )
            x = np.float64((matrix[0, 1] + matrix[1, 0]) / scale)
            y = np.float64(0.25 * scale)
            z = np.float64((matrix[1, 2] + matrix[2, 1]) / scale)
            w = np.float64((matrix[0, 2] - matrix[2, 0]) / scale)
        else:
            scale = np.float64(
                math.sqrt(float(np.float64(1.0 + diagonal[2] - diagonal[0] - diagonal[1])))
                * 2.0
            )
            x = np.float64((matrix[0, 2] + matrix[2, 0]) / scale)
            y = np.float64((matrix[1, 2] + matrix[2, 1]) / scale)
            z = np.float64(0.25 * scale)
            w = np.float64((matrix[1, 0] - matrix[0, 1]) / scale)
    quaternion = np.asarray((x, y, z, w), dtype=np.float64)
    squared_norm = np.float64(0.0)
    for component in quaternion:
        squared_norm = np.float64(squared_norm + np.float64(component * component))
    norm = np.float64(math.sqrt(float(squared_norm)))
    if not np.isfinite(norm) or norm == np.float64(0.0):
        _fail("rotation-to-quaternion conversion is singular")
    for index in range(4):
        quaternion[index] = np.float64(quaternion[index] / norm)
    for component in (quaternion[3], quaternion[0], quaternion[1], quaternion[2]):
        if component != np.float64(0.0):
            if component < np.float64(0.0):
                quaternion *= np.float64(-1.0)
            break
    sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(quaternion)
    return quaternion


def evaluate_sequence_request(document: Any) -> bytes:
    """Execute the complete frozen common-population CP2-D mathematics."""

    try:
        request = transport.decode_sequence_request(document)
    except transport.SequenceMathCodecError as exc:
        raise SequenceMathWorkerError(str(exc)) from exc
    index = request.sequence_index

    decoded = {
        "nullspace": (
            request.nullspace_timestamps_ns,
            np.asarray(request.nullspace_positions.values, dtype=np.float64).reshape(
                request.nullspace_positions.shape
            ),
            np.asarray(
                request.nullspace_quaternions_xyzw.values, dtype=np.float64
            ).reshape(request.nullspace_quaternions_xyzw.shape),
        ),
        "schur": (
            request.schur_timestamps_ns,
            np.asarray(request.schur_positions.values, dtype=np.float64).reshape(
                request.schur_positions.shape
            ),
            np.asarray(request.schur_quaternions_xyzw.values, dtype=np.float64).reshape(
                request.schur_quaternions_xyzw.shape
            ),
        ),
    }
    gt_timestamps = request.ground_truth_timestamps_ns
    gt_positions_all = np.asarray(
        request.ground_truth_positions.values, dtype=np.float64
    ).reshape(request.ground_truth_positions.shape)
    gt_quaternions = np.asarray(
        request.ground_truth_quaternions_xyzw.values, dtype=np.float64
    ).reshape(request.ground_truth_quaternions_xyzw.shape)
    # Validate every quaternion even though CP2-D's translation ATE uses only
    # positions; malformed orientation input cannot hide in the retained join.
    for row in gt_quaternions:
        sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(row)

    full_shared = sequence_math.shared_timestamp_intersection(
        decoded["nullspace"][0], decoded["schur"][0]
    )
    if len(full_shared) < 3:
        _fail("shared trajectory population contains fewer than three poses")
    associations = sequence_math.associate_nearest_ground_truth(
        full_shared, gt_timestamps
    )
    if len(associations) < 3:
        _fail("associated shared trajectory contains fewer than three poses")
    shared = tuple(item.estimator_timestamp_ns for item in associations)
    mode_indices = {
        mode: {timestamp: ordinal for ordinal, timestamp in enumerate(decoded[mode][0])}
        for mode in MODES
    }
    null_positions = np.asarray(
        [decoded["nullspace"][1][mode_indices["nullspace"][timestamp]] for timestamp in shared],
        dtype=np.float64,
    )
    null_quaternions = np.asarray(
        [decoded["nullspace"][2][mode_indices["nullspace"][timestamp]] for timestamp in shared],
        dtype=np.float64,
    )
    schur_positions = np.asarray(
        [decoded["schur"][1][mode_indices["schur"][timestamp]] for timestamp in shared],
        dtype=np.float64,
    )
    schur_quaternions = np.asarray(
        [decoded["schur"][2][mode_indices["schur"][timestamp]] for timestamp in shared],
        dtype=np.float64,
    )
    gt_positions = np.asarray(
        [gt_positions_all[item.ground_truth_index] for item in associations], dtype=np.float64
    )
    alignment = sequence_math.baseline_kabsch_alignment(null_positions, gt_positions)
    aligned = sequence_math.apply_common_alignment(
        alignment,
        null_positions,
        null_quaternions,
        schur_positions,
        schur_quaternions,
    )
    position_differences = sequence_math.position_differences_m(
        aligned.nullspace_positions, aligned.schur_positions
    )
    orientation_differences = sequence_math.orientation_differences_deg(
        aligned.nullspace_inverse_rotations, aligned.schur_inverse_rotations
    )
    position_p95 = sequence_math.linear_p95(position_differences)
    orientation_p95 = sequence_math.linear_p95(orientation_differences)
    ate_nullspace = sequence_math.translation_rmse_m(
        aligned.nullspace_positions, gt_positions
    )
    ate_schur = sequence_math.translation_rmse_m(aligned.schur_positions, gt_positions)
    relative_ate = sequence_math.relative_ate_difference(ate_nullspace, ate_schur)
    sequence_math.validate_metric_limits(position_p95, orientation_p95, relative_ate)
    alignment_quaternion = _rotation_quaternion_xyzw(alignment.rotation)
    nullspace_aligned_quaternions = np.asarray(
        [
            _rotation_quaternion_xyzw(aligned.nullspace_inverse_rotations[index])
            for index in range(len(shared))
        ],
        dtype=np.float64,
    )
    schur_aligned_quaternions = np.asarray(
        [
            _rotation_quaternion_xyzw(aligned.schur_inverse_rotations[index])
            for index in range(len(shared))
        ],
        dtype=np.float64,
    )

    response = {
        "schema_version": SCHEMA_VERSION,
        "record_type": RESPONSE_RECORD_TYPE,
        "sequence_index": index,
        "sequence_id": request.sequence_id,
        "request_sha256": hashlib.sha256(document).hexdigest(),
        "shared_timestamps_ns": list(shared),
        "associations": [
            {
                "estimator_index": item.estimator_index,
                "estimator_timestamp_ns": item.estimator_timestamp_ns,
                "ground_truth_index": item.ground_truth_index,
                "ground_truth_timestamp_ns": item.ground_truth_timestamp_ns,
                "absolute_difference_ns": item.absolute_difference_ns,
            }
            for item in associations
        ],
        "alignment": {
            "rotation": _array(alignment.rotation, (3, 3)),
            "translation": _array(alignment.translation, (3,)),
            "quaternion_xyzw": _array(alignment_quaternion, (4,)),
            "source_count": alignment.source_count,
            "source_singular_values": _array(alignment.source_singular_values, (3,)),
            "source_rank_threshold_bits": _scalar(alignment.source_rank_threshold),
            "cross_covariance_singular_values": _array(
                alignment.cross_covariance_singular_values, (3,)
            ),
            "cross_covariance_rank_threshold_bits": _scalar(
                alignment.cross_covariance_rank_threshold
            ),
            "reflection_correction_applied": (
                alignment.reflection_correction_applied
            ),
            "determinant_bits": _scalar(alignment.determinant),
            "orthogonality_error_frobenius_bits": _scalar(
                alignment.orthogonality_error_frobenius
            ),
            "applied_identically_to_both_modes": True,
        },
        "ground_truth_associated_positions": _array(gt_positions),
        "nullspace_aligned_positions": _array(aligned.nullspace_positions),
        "schur_aligned_positions": _array(aligned.schur_positions),
        "nullspace_aligned_quaternions_xyzw": _array(
            nullspace_aligned_quaternions
        ),
        "schur_aligned_quaternions_xyzw": _array(schur_aligned_quaternions),
        "nullspace_aligned_inverse_rotations": _array(
            aligned.nullspace_inverse_rotations
        ),
        "schur_aligned_inverse_rotations": _array(aligned.schur_inverse_rotations),
        "position_differences_m": _array(position_differences),
        "orientation_differences_deg": _array(orientation_differences),
        "metrics": {
            "position_p95_m_bits": _scalar(position_p95),
            "orientation_p95_deg_bits": _scalar(orientation_p95),
            "ate_nullspace_m_bits": _scalar(ate_nullspace),
            "ate_schur_m_bits": _scalar(ate_schur),
            "relative_ate_difference_bits": _scalar(relative_ate),
        },
    }
    payload = _canonical(response)
    try:
        transport.decode_sequence_response(payload, document)
    except transport.SequenceMathCodecError as exc:
        raise SequenceMathWorkerError("self-invalid direct response: " + str(exc)) from exc
    return payload


def _kat_input(case: direct_kat.KatCase, name: str) -> np.ndarray:
    for item in case.inputs:
        if item.name == name:
            values = [f64_codec.bits_hex_to_f64(bits) for bits in item.bits]
            return np.asarray(values, dtype=np.float64).reshape(item.shape)
    _fail("direct-KAT case lacks frozen input " + name)


def _kat_output(name: str, shape: Tuple[int, ...], value: Any) -> Mapping[str, Any]:
    encoded = _array(value, shape)
    return {"name": name, "shape": encoded["shape"], "bits": encoded["bits"]}


def _kat_input_record(item: direct_kat.KatOutput) -> Mapping[str, Any]:
    return {"name": item.name, "shape": list(item.shape), "bits": list(item.bits)}


def _evaluate_kat_repeat(
    request: direct_kat.DirectKatRequest,
) -> Sequence[Mapping[str, Any]]:
    results = []
    proper_alignment = None
    for case in request.cases:
        if case.name == "kabsch_proper_full_rank":
            proper_alignment = sequence_math.baseline_kabsch_alignment(
                _kat_input(case, "source_positions"),
                _kat_input(case, "target_positions"),
            )
            outputs = (
                _kat_output("rotation_row_major", (3, 3), proper_alignment.rotation),
                _kat_output("translation", (3,), proper_alignment.translation),
                _kat_output(
                    "source_singular_values", (3,), proper_alignment.source_singular_values
                ),
                _kat_output(
                    "source_rank_threshold", (), proper_alignment.source_rank_threshold
                ),
                _kat_output(
                    "cross_covariance_singular_values",
                    (3,),
                    proper_alignment.cross_covariance_singular_values,
                ),
                _kat_output(
                    "cross_covariance_rank_threshold",
                    (),
                    proper_alignment.cross_covariance_rank_threshold,
                ),
                _kat_output(
                    "determinant_correction_sign",
                    (),
                    np.float64(
                        -1.0
                        if proper_alignment.reflection_correction_applied
                        else 1.0
                    ),
                ),
                _kat_output("determinant", (), proper_alignment.determinant),
                _kat_output(
                    "orthogonality_error_frobenius",
                    (),
                    proper_alignment.orthogonality_error_frobenius,
                ),
            )
        elif case.name == "kabsch_reflection_correction":
            alignment = sequence_math.baseline_kabsch_alignment(
                _kat_input(case, "source_positions"),
                _kat_input(case, "target_positions"),
            )
            outputs = (
                _kat_output("rotation_row_major", (3, 3), alignment.rotation),
                _kat_output("translation", (3,), alignment.translation),
                _kat_output(
                    "source_singular_values", (3,), alignment.source_singular_values
                ),
                _kat_output("source_rank_threshold", (), alignment.source_rank_threshold),
                _kat_output(
                    "cross_covariance_singular_values",
                    (3,),
                    alignment.cross_covariance_singular_values,
                ),
                _kat_output(
                    "cross_covariance_rank_threshold",
                    (),
                    alignment.cross_covariance_rank_threshold,
                ),
                _kat_output(
                    "determinant_correction_sign",
                    (),
                    np.float64(
                        -1.0
                        if alignment.reflection_correction_applied
                        else 1.0
                    ),
                ),
                _kat_output("determinant", (), alignment.determinant),
                _kat_output(
                    "orthogonality_error_frobenius",
                    (),
                    alignment.orthogonality_error_frobenius,
                ),
            )
        elif case.name == "common_alignment_matrix_products":
            if proper_alignment is None:
                _fail("direct-KAT common-alignment case did not follow case one")
            source = _kat_input(case, "nullspace_source_positions")
            delta = _kat_input(case, "schur_position_delta")
            stored = _kat_input(case, "schur_stored_quaternion_xyzw")
            schur = np.empty_like(source)
            for row in range(source.shape[0]):
                for column in range(3):
                    schur[row, column] = np.float64(source[row, column] + delta[column])
            identity = np.tile(
                np.asarray((0.0, 0.0, 0.0, 1.0), dtype=np.float64),
                (source.shape[0], 1),
            )
            schur_quaternions = np.tile(stored, (source.shape[0], 1))
            aligned = sequence_math.apply_common_alignment(
                proper_alignment,
                source,
                identity,
                schur,
                schur_quaternions,
            )
            stored_rotation = sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(
                stored
            )
            # This repeats the exact production matrix-product expression on a
            # dedicated output so the selected BLAS/NumPy stack's rounding is
            # visible even if a later aggregate happens to mask one component.
            discriminator = np.matmul(
                proper_alignment.rotation, stored_rotation, dtype=np.float64
            )
            outputs = (
                _kat_output(
                    "alignment_rotation_row_major", (3, 3), proper_alignment.rotation
                ),
                _kat_output("alignment_translation", (3,), proper_alignment.translation),
                _kat_output(
                    "nullspace_aligned_positions", (6, 3), aligned.nullspace_positions
                ),
                _kat_output("schur_aligned_positions", (6, 3), aligned.schur_positions),
                _kat_output(
                    "nullspace_aligned_inverse_rotations",
                    (6, 3, 3),
                    aligned.nullspace_inverse_rotations,
                ),
                _kat_output(
                    "schur_aligned_inverse_rotations",
                    (6, 3, 3),
                    aligned.schur_inverse_rotations,
                ),
                _kat_output("matmul_rounding_discriminator", (3, 3), discriminator),
            )
        elif case.name == "ordered_translation_rmse":
            value = sequence_math.translation_rmse_m(
                _kat_input(case, "aligned_positions"),
                _kat_input(case, "ground_truth_positions"),
            )
            outputs = (_kat_output("translation_rmse", (), value),)
        elif case.name == "linear_p95_boundary":
            quantile = _kat_input(case, "quantile")
            if quantile.shape != () or _scalar(quantile.item()) != "3fee666666666666":
                _fail("direct-KAT p95 quantile differs from the frozen 0.95 bits")
            outputs = (
                _kat_output(
                    "boundary_p95",
                    (),
                    sequence_math.linear_p95(_kat_input(case, "boundary_values")),
                ),
                _kat_output(
                    "unsorted_companion_p95",
                    (),
                    sequence_math.linear_p95(
                        _kat_input(case, "unsorted_companion_values")
                    ),
                ),
            )
        else:
            _fail("direct-KAT case identity differs")
        if tuple(item["name"] for item in outputs) != tuple(
            name for name, _ in direct_kat.OUTPUT_SPECS[case.case_index]
        ):
            _fail("internal direct-KAT output order differs")
        results.append(
            {
                "case_index": case.case_index,
                "name": case.name,
                "inputs": [_kat_input_record(item) for item in case.inputs],
                "outputs": list(outputs),
            }
        )
    return results


def evaluate_direct_kat(request_document: Any, expectation_document: Any) -> bytes:
    """Run the frozen five-case bundle twice and require reviewed answer bits."""

    request = direct_kat.decode_request(request_document)
    expectation = direct_kat.decode_expectation(expectation_document)
    if request.canonical_bytes != direct_kat.encode_request_from_expectation(
        expectation.canonical_bytes
    ):
        _fail("direct-KAT request is not the projection of its known answers")
    response = {
        "schema_version": direct_kat.SCHEMA_VERSION,
        "record_type": direct_kat.RESPONSE_RECORD_TYPE,
        "codec": direct_kat.CODEC,
        "repeat_count": direct_kat.REPEAT_COUNT,
        "case_order": list(direct_kat.CASE_ORDER),
        "repeats": [
            {"repeat_index": repeat_index, "cases": list(_evaluate_kat_repeat(request))}
            for repeat_index in range(direct_kat.REPEAT_COUNT)
        ],
    }
    payload = _canonical(response)
    try:
        direct_kat.validate_pair(expectation.canonical_bytes, payload)
    except direct_kat.DirectKatError as exc:
        raise SequenceMathWorkerError("direct-KAT response: " + str(exc)) from exc
    return payload


def derive_direct_kat_expectation(request_document: Any) -> Tuple[bytes, bytes]:
    """Produce review candidates from one selected stack before source freeze.

    This helper is deliberately absent from the executable CLI.  It is only a
    data-free construction surface: the resulting expectation must still be
    reviewed, inventoried in the capsule, and then reproduced by the formal
    preflight and detached verifier.
    """

    request = direct_kat.decode_request(request_document)
    repeats = [list(_evaluate_kat_repeat(request)) for _ in range(direct_kat.REPEAT_COUNT)]
    if repeats[0] != repeats[1]:
        _fail("direct-KAT construction repeats are not bit-identical")
    expectation = _canonical(
        {
            "schema_version": direct_kat.SCHEMA_VERSION,
            "record_type": direct_kat.EXPECTATION_RECORD_TYPE,
            "codec": direct_kat.CODEC,
            "repeat_count": direct_kat.REPEAT_COUNT,
            "case_order": list(direct_kat.CASE_ORDER),
            "cases": repeats[0],
        }
    )
    response = _canonical(
        {
            "schema_version": direct_kat.SCHEMA_VERSION,
            "record_type": direct_kat.RESPONSE_RECORD_TYPE,
            "codec": direct_kat.CODEC,
            "repeat_count": direct_kat.REPEAT_COUNT,
            "case_order": list(direct_kat.CASE_ORDER),
            "repeats": [
                {"repeat_index": index, "cases": cases}
                for index, cases in enumerate(repeats)
            ],
        }
    )
    try:
        direct_kat.decode_expectation(expectation)
        direct_kat.validate_pair(expectation, response)
    except direct_kat.DirectKatError as exc:
        raise SequenceMathWorkerError("constructed direct-KAT: " + str(exc)) from exc
    return expectation, response


def validate_runtime_environment(environment: Mapping[str, str]) -> None:
    if type(environment) not in (dict, os._Environ):
        _fail("direct-math environment has the wrong mapping type")
    expected_names = set(REQUIRED_FIXED_ENVIRONMENT) | set(PRIVATE_ENVIRONMENT_SUFFIXES)
    if set(environment) != expected_names:
        _fail("direct-math environment key inventory differs")
    for name, expected in REQUIRED_FIXED_ENVIRONMENT.items():
        if environment.get(name) != expected:
            _fail("direct-math fixed environment differs: " + name)
    present = sorted(name for name in FORBIDDEN_INJECTION_ENVIRONMENT if name in environment)
    if present:
        _fail("direct-math environment contains injection variables: " + repr(present))
    private_root = None
    for name, suffix in PRIVATE_ENVIRONMENT_SUFFIXES.items():
        value = environment.get(name)
        if type(value) is not str or not value or "\0" in value:
            _fail("direct-math private environment path differs: " + name)
        path = Path(value)
        if not path.is_absolute() or os.path.normpath(value) != value or path.name != suffix:
            _fail("direct-math private environment path differs: " + name)
        if private_root is None:
            private_root = path.parent
        elif path.parent != private_root:
            _fail("direct-math private environment paths do not share one root")


def _capsule_root() -> Path:
    module = Path(__file__).resolve(strict=True)
    for ancestor in module.parents:
        if (
            ancestor.name == "python3.11"
            and ancestor.parent.name == "lib"
            and ancestor.parent.parent.name == "python"
        ):
            return ancestor.parent.parent.parent
    _fail("direct-math module is outside the frozen capsule layout")


def validate_import_isolation() -> None:
    root = _capsule_root()
    root_text = str(root) + os.path.sep
    for item in sys.path:
        if not item:
            _fail("direct-math import path contains the current directory")
        resolved = str(Path(item).resolve(strict=False))
        if resolved != str(root) and not resolved.startswith(root_text):
            _fail("direct-math import path escapes the capsule: " + resolved)
        if item.endswith((".zip", ".egg")) and Path(item).exists():
            _fail("direct-math import path contains an opaque archive")
    for name, module in sorted(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        resolved = str(Path(origin).resolve(strict=False))
        if resolved != str(root) and not resolved.startswith(root_text):
            _fail("direct-math loaded module escapes the capsule: " + name)


def validate_loader_maps() -> None:
    root = _capsule_root()
    root_text = str(root) + os.path.sep
    try:
        lines = Path("/proc/self/maps").read_text(encoding="ascii", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise SequenceMathWorkerError("direct-math loader maps cannot be read") from exc
    mapped = []
    for line in lines:
        fields = line.split(None, 5)
        if len(fields) < 6 or not fields[5].startswith("/"):
            continue
        if fields[5].endswith(" (deleted)"):
            _fail("direct-math retains a deleted file mapping")
        resolved = str(Path(fields[5]).resolve(strict=True))
        if resolved != str(root) and not resolved.startswith(root_text):
            _fail("direct-math loader mapping escapes the capsule: " + resolved)
        mapped.append(resolved)
    if not mapped:
        _fail("direct-math loader map inventory is empty")


def validate_numerical_thread_state() -> None:
    library = (
        _capsule_root()
        / "python"
        / "lib"
        / "python3.11"
        / "numpy.libs"
        / "libscipy_openblas64_-32a4b2a6.so"
    )
    try:
        status_value = os.stat(str(library), follow_symlinks=False)
    except OSError as exc:
        raise SequenceMathWorkerError(
            "selected NumPy OpenBLAS library is absent"
        ) from exc
    if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink not in (0, 1):
        _fail("selected NumPy OpenBLAS library identity differs")
    library_sha256 = hashlib.sha256(_read_regular(library)).hexdigest()
    if library_sha256 != OPENBLAS_LIBRARY_SHA256:
        _fail("selected NumPy OpenBLAS library digest differs")
    try:
        identity = fp_control.openblas_identity(str(library))
    except (AttributeError, RuntimeError) as exc:
        raise SequenceMathWorkerError(
            "selected NumPy OpenBLAS runtime identity cannot be read"
        ) from exc
    if type(identity) is not dict or any(
        identity.get(name) != expected
        for name, expected in EXPECTED_OPENBLAS_IDENTITY.items()
    ):
        _fail("direct-math selected NumPy OpenBLAS identity differs")
    if type(identity.get("num_procs")) is not int or identity["num_procs"] <= 0:
        _fail("direct-math selected NumPy OpenBLAS processor count is invalid")
    if identity["threads"] != 1:
        _fail("direct-math selected NumPy OpenBLAS is not single-threaded")
    try:
        machine = fp_control.x86_cpuid_identity()
    except (AttributeError, RuntimeError) as exc:
        raise SequenceMathWorkerError("direct-math CPUID identity cannot be read") from exc
    if type(machine) is not dict or any(
        machine.get(name) != expected for name, expected in EXPECTED_X86_MACHINE.items()
    ):
        _fail("direct-math x86 machine identity differs")
    # The explicitly frozen SkylakeX OpenBLAS core requires an OS-enabled
    # AVX-512 register state and the common Skylake-X feature subset.  Exact
    # raw CPUID leaves are retained in the capsule receipt; these masks are the
    # fail-closed execution requirement rather than a mutable autodispatch.
    if (machine.get("leaf1_ecx", 0) & 0x18001000) != 0x18001000:
        _fail("direct-math x86 AVX/FMA/OSXSAVE feature mask differs")
    if (machine.get("leaf7_ebx", 0) & 0xD0030028) != 0xD0030028:
        _fail("direct-math x86 AVX2/AVX-512 feature mask differs")
    if (machine.get("xcr0", 0) & 0xE6) != 0xE6:
        _fail("direct-math x86 AVX-512 state is not OS-enabled")
    cpu_features = getattr(np_cpu_dispatch, "__cpu_features__", None)
    cpu_baseline = getattr(np_cpu_dispatch, "__cpu_baseline__", None)
    cpu_dispatch = getattr(np_cpu_dispatch, "__cpu_dispatch__", None)
    if cpu_baseline != EXPECTED_NUMPY_BASELINE or cpu_dispatch != EXPECTED_NUMPY_DISPATCH:
        _fail("direct-math NumPy CPU target inventory differs")
    if type(cpu_features) is not dict or not cpu_features.get("X86_V2") or any(
        cpu_features.get(name, False)
        for name in ("X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR")
    ):
        _fail("direct-math NumPy dispatch disablement differs")


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(
        str(path), os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = os.fstat(descriptor)
        by_path = os.stat(str(path), follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or (
                before.st_nlink != 1
                and (
                    before.st_nlink != 0
                    or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
                    != REQUIRED_MEMFD_SEALS
                )
            )
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o444
            or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
            or before.st_size <= 0
            or before.st_size > MAX_DOCUMENT_BYTES
        ):
            _fail("direct-math request is not one bounded regular file")
        payload = bytearray()
        while len(payload) < before.st_size:
            block = os.read(descriptor, min(1 << 20, before.st_size - len(payload)))
            if not block:
                _fail("direct-math request became short")
            payload.extend(block)
        if os.read(descriptor, 1):
            _fail("direct-math request grew during read")
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            _fail("direct-math request changed during read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> None:
    parent = path.parent
    parent_status = os.stat(str(parent), follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_status.st_mode)
        or parent_status.st_uid != os.geteuid()
        or stat.S_IMODE(parent_status.st_mode) != 0o700
    ):
        _fail("direct-math response parent is not one private directory")
    parent_fd = os.open(
        str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _fail("direct-math response write made no progress")
            offset += written
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        status_value = os.fstat(descriptor)
        by_path = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (status_value.st_dev, status_value.st_ino)
            != (by_path.st_dev, by_path.st_ino)
            or by_path.st_nlink != 1
            or stat.S_IMODE(by_path.st_mode) != 0o444
            or by_path.st_size != len(payload)
        ):
            _fail("direct-math response identity differs after write")
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _write_held(path: Path, payload: bytes) -> None:
    """Fill the sandbox-private output inode exported to the parent.

    The static sandbox cannot bind-mount an anonymous memfd on this kernel, so
    it creates one regular file in its private tmpfs and copies that file into
    the parent-held response memfd only after the PID namespace is empty.  A
    legacy direct memfd transport remains accepted for protecting tests.
    """

    descriptor = os.open(
        str(path), os.O_WRONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        before = os.fstat(descriptor)
        by_path = os.stat(str(path), follow_symlinks=False)
        try:
            seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            seals = None
        held_kind = (
            (before.st_nlink == 0 and seals == 0)
            or (
                before.st_nlink == 1
                and seals in (None, fcntl.F_SEAL_SEAL)
            )
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or not held_kind
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != 0
            or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
        ):
            _fail("direct-math held response identity differs")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                _fail("direct-math held response write made no progress")
            offset += written
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        by_path = os.stat(str(path), follow_symlinks=False)
        try:
            after_seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            after_seals = None
        if (
            (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or (by_path.st_dev, by_path.st_ino) != (before.st_dev, before.st_ino)
            or after.st_nlink != before.st_nlink
            or stat.S_IMODE(after.st_mode) != 0o444
            or after.st_size != len(payload)
            or after_seals != seals
        ):
            _fail("direct-math held response differs after write")
    finally:
        os.close(descriptor)


def main(arguments: Sequence[str]) -> int:
    try:
        if not sys.flags.isolated:
            _fail("CP2-D direct-math worker requires isolated Python (-I)")
        if fp_control.CAPSULE_NATIVE != 1:
            _fail("CP2-D direct-math worker lacks its native FP probe")
        validate_runtime_environment(os.environ)
        validate_import_isolation()
        validate_loader_maps()
        # The static capsule launcher establishes these controls before the
        # interpreter starts.  Probe again after Python/NumPy imports and once
        # more after all requested arithmetic; sticky status bits are cleared
        # only after their control bits have been checked.
        fp_control.verify()
        # Gradual binary64 underflow is part of the frozen formulas: tiny
        # products and thresholds must round to subnormal values (or zero)
        # rather than becoming an out-of-contract execution failure.  Invalid,
        # divide-by-zero, and overflow remain fail-closed.
        np.seterr(
            divide="raise", over="raise", invalid="raise", under="ignore"
        )
        validate_numerical_thread_state()
        if tuple(arguments) == ("--version",):
            fp_control.verify()
            sys.stdout.write(
                "cp2-direct-math-worker-v1 python={}.{}.{} numpy={}\n".format(
                    sys.version_info.major,
                    sys.version_info.minor,
                    sys.version_info.micro,
                    np.__version__,
                )
            )
            return 0
        kat_mode = (
            len(arguments) == 6
            and tuple(arguments[0::2])
            == ("--input", "--known-answers", "--output")
        )
        sequence_mode = (
            len(arguments) == 4
            and tuple(arguments[0::2]) == ("--input", "--output")
        )
        if not sequence_mode and not kat_mode:
            sys.stderr.write("CP2-D direct-math worker rejected its exact CLI\n")
            return 2
        input_path = Path(arguments[1])
        known_path = Path(arguments[3]) if kat_mode else None
        output_path = Path(arguments[5] if kat_mode else arguments[3])
        paths = [("input", input_path), ("output", output_path)]
        if known_path is not None:
            paths.insert(1, ("known answers", known_path))
        for label, path in paths:
            text = str(path)
            if not path.is_absolute() or os.path.normpath(text) != text or text == os.path.sep:
                _fail("direct-math " + label + " path is not normalized absolute")
        request = _read_regular(input_path)
        if known_path is None:
            response = evaluate_sequence_request(request)
        else:
            response = evaluate_direct_kat(request, _read_regular(known_path))
        if output_path.exists():
            _write_held(output_path, response)
        else:
            _write_new(output_path, response)
        validate_loader_maps()
        fp_control.verify()
    except BaseException as exc:
        sys.stderr.write(
            "CP2-D direct-math worker failed closed: {}: {}\n".format(
                type(exc).__name__, exc
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = [
    "MAX_DOCUMENT_BYTES",
    "REQUEST_RECORD_TYPE",
    "RESPONSE_RECORD_TYPE",
    "SequenceMathWorkerError",
    "derive_direct_kat_expectation",
    "evaluate_direct_kat",
    "evaluate_sequence_request",
    "validate_runtime_environment",
    "validate_import_isolation",
    "validate_loader_maps",
    "validate_numerical_thread_state",
]
