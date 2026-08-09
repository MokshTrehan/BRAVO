#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stdlib-only canonical transport for the CP2-D direct-math capsule.

This module deliberately imports no numerical package and performs no
trajectory/alignment arithmetic.  It only encodes already-retained native
binary64 pose components as exact bit strings and validates the capsule's
canonical request/response structure, integer joins, and bit-preserving source
projections.  Authoritative floating-point work remains inside the separately
inventoried capsule worker.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import struct
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


SCHEMA_VERSION = 1
REQUEST_RECORD_TYPE = "cp2_d_sequence_math_request"
RESPONSE_RECORD_TYPE = "cp2_d_sequence_math_response"
MAX_DOCUMENT_BYTES = 256 << 20
MAX_POSE_COUNT = 1_000_000
MAX_ARRAY_ELEMENTS = 9 * MAX_POSE_COUNT
GROUND_TRUTH_MAX_DIFFERENCE_NS = 10_000_000
QUATERNION_NORM_TOLERANCE = 1.0e-3
SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
MODES = ("nullspace", "schur")
_BITS = re.compile(r"^[0-9a-f]{16}$")


class SequenceMathCodecError(ValueError):
    """Raised when the bit-exact capsule transport is not self-consistent."""


def _fail(message: str) -> None:
    raise SequenceMathCodecError(message)


def _exact(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    expected = set(keys)
    if type(value) is not dict or set(value) != expected or len(value) != len(expected):
        _fail(label + " key inventory differs")
    return value


def _reject_float(token: str) -> None:
    _fail("JSON floating-point values are forbidden: " + token)


def _reject_constant(token: str) -> None:
    _fail("non-JSON numeric constants are forbidden: " + token)


def _pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate JSON key: " + key)
        value[key] = item
    return value


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
        raise SequenceMathCodecError("math transport is not canonical JSON data") from exc
    if not payload or len(payload) > MAX_DOCUMENT_BYTES:
        _fail("math transport exceeds its frozen size bound")
    return payload


def _parse(document: Any, label: str) -> Mapping[str, Any]:
    if type(document) is not bytes or not document or len(document) > MAX_DOCUMENT_BYTES:
        _fail(label + " byte size/type is invalid")
    try:
        value = json.loads(
            document.decode("ascii", "strict"),
            object_pairs_hook=_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except SequenceMathCodecError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise SequenceMathCodecError(label + " is not strict canonical JSON") from exc
    if type(value) is not dict or _canonical(value) != document:
        _fail(label + " is not the unique canonical encoding")
    return value


def _u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > (1 << 64) - 1:
        _fail(label + " is not u64")
    return value


def _timestamps(value: Any, label: str, *, unique: bool) -> Tuple[int, ...]:
    if type(value) not in (list, tuple) or len(value) > MAX_POSE_COUNT:
        _fail(label + " must be one bounded timestamp array")
    result = tuple(_u64(item, label + " timestamp") for item in value)
    for index in range(1, len(result)):
        if result[index] < result[index - 1] or (
            unique and result[index] == result[index - 1]
        ):
            _fail(label + " timestamps reverse or violate uniqueness")
    return result


def f64_to_bits(value: Any, label: str = "binary64 value") -> str:
    if type(value) is not float or not math.isfinite(value):
        _fail(label + " must be one finite native binary64")
    return struct.pack(">d", value).hex()


def bits_to_f64(value: Any, label: str = "binary64 bits") -> float:
    if type(value) is not str or _BITS.fullmatch(value) is None:
        _fail(label + " must be exactly 16 lowercase hexadecimal digits")
    result = struct.unpack(">d", bytes.fromhex(value))[0]
    if not math.isfinite(result):
        _fail(label + " encodes a nonfinite binary64")
    return result


def validate_stored_quaternion(value: Sequence[float], label: str) -> Tuple[float, ...]:
    if type(value) not in (list, tuple) or len(value) != 4:
        _fail(label + " must contain four binary64 components")
    components = tuple(
        item if type(item) is float and math.isfinite(item) else None for item in value
    )
    if any(item is None for item in components):
        _fail(label + " must contain four finite native binary64 components")
    squared = 0.0
    for component in components:
        squared = float(squared + float(component * component))  # type: ignore[operator]
    norm = math.sqrt(squared)
    if (
        norm < 1.0 - QUATERNION_NORM_TOLERANCE
        or norm > 1.0 + QUATERNION_NORM_TOLERANCE
    ):
        _fail(label + " norm is outside [1-1e-3,1+1e-3]")
    return components  # type: ignore[return-value]


def _element_count(shape: Tuple[int, ...], label: str) -> int:
    count = 1
    for dimension in shape:
        if type(dimension) is not int or dimension < 0:
            _fail(label + " has an invalid dimension")
        if count and dimension > MAX_ARRAY_ELEMENTS // count:
            _fail(label + " element count exceeds its frozen bound")
        count *= dimension
    if count > MAX_ARRAY_ELEMENTS:
        _fail(label + " element count exceeds its frozen bound")
    return count


@dataclass(frozen=True)
class F64BitsArray:
    shape: Tuple[int, ...]
    bits: Tuple[str, ...]

    @property
    def values(self) -> Tuple[float, ...]:
        return tuple(bits_to_f64(item) for item in self.bits)


def _array_record(value: Any, expected_shape: Tuple[int, ...], label: str) -> F64BitsArray:
    record = _exact(value, ("shape", "bits"), label)
    if type(record["shape"]) is not list or tuple(record["shape"]) != expected_shape:
        _fail(label + " shape differs")
    if type(record["bits"]) is not list:
        _fail(label + " bits must be an array")
    expected_count = _element_count(expected_shape, label)
    if len(record["bits"]) != expected_count:
        _fail(label + " bit population differs from its shape")
    bits = tuple(record["bits"])
    for index, item in enumerate(bits):
        bits_to_f64(item, "{} bits[{}]".format(label, index))
    return F64BitsArray(expected_shape, bits)


def _flat_rows(value: Any, rows: int, columns: int, label: str) -> Mapping[str, Any]:
    if type(value) not in (list, tuple) or len(value) != rows:
        _fail(label + " row population differs")
    bits = []
    for row_index, row in enumerate(value):
        if type(row) not in (list, tuple) or len(row) != columns:
            _fail(label + " row shape differs")
        for column_index, item in enumerate(row):
            bits.append(
                f64_to_bits(
                    item,
                    "{} row {} column {}".format(label, row_index, column_index),
                )
            )
    _element_count((rows, columns), label)
    return {"shape": [rows, columns], "bits": bits}


@dataclass(frozen=True)
class SequenceMathRequest:
    sequence_index: int
    sequence_id: str
    nullspace_timestamps_ns: Tuple[int, ...]
    nullspace_positions: F64BitsArray
    nullspace_quaternions_xyzw: F64BitsArray
    schur_timestamps_ns: Tuple[int, ...]
    schur_positions: F64BitsArray
    schur_quaternions_xyzw: F64BitsArray
    ground_truth_timestamps_ns: Tuple[int, ...]
    ground_truth_positions: F64BitsArray
    ground_truth_quaternions_xyzw: F64BitsArray
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class SequenceMathResponse:
    sequence_index: int
    sequence_id: str
    request_sha256: str
    shared_timestamps_ns: Tuple[int, ...]
    associations: Tuple[Mapping[str, int], ...]
    arrays: Mapping[str, F64BitsArray]
    alignment_arrays: Mapping[str, F64BitsArray]
    alignment_scalars: Mapping[str, str]
    reflection_correction_applied: bool
    metric_bits: Mapping[str, str]
    canonical_bytes: bytes
    sha256: str

    def metric(self, name: str) -> float:
        if name not in self.metric_bits:
            _fail("direct-math response lacks metric " + name)
        return bits_to_f64(self.metric_bits[name], "metric " + name)


def encode_sequence_request(
    *,
    sequence_index: int,
    sequence_id: str,
    nullspace_timestamps_ns: Sequence[int],
    nullspace_positions: Sequence[Sequence[float]],
    nullspace_quaternions_xyzw: Sequence[Sequence[float]],
    schur_timestamps_ns: Sequence[int],
    schur_positions: Sequence[Sequence[float]],
    schur_quaternions_xyzw: Sequence[Sequence[float]],
    ground_truth_timestamps_ns: Sequence[int],
    ground_truth_positions: Sequence[Sequence[float]],
    ground_truth_quaternions_xyzw: Sequence[Sequence[float]],
) -> bytes:
    """Encode retained poses without importing or invoking a numerical stack."""

    index = _u64(sequence_index, "request sequence index")
    if index >= len(SEQUENCES) or sequence_id != SEQUENCES[index]:
        _fail("request sequence identity differs from the frozen inventory")
    null_timestamps = _timestamps(nullspace_timestamps_ns, "nullspace", unique=True)
    schur_timestamps = _timestamps(schur_timestamps_ns, "schur", unique=True)
    gt_timestamps = _timestamps(ground_truth_timestamps_ns, "ground truth", unique=False)
    document = {
        "schema_version": SCHEMA_VERSION,
        "record_type": REQUEST_RECORD_TYPE,
        "sequence_index": index,
        "sequence_id": sequence_id,
        "nullspace": {
            "timestamps_ns": list(null_timestamps),
            "positions": _flat_rows(
                nullspace_positions, len(null_timestamps), 3, "nullspace positions"
            ),
            "stored_quaternions_xyzw": _flat_rows(
                nullspace_quaternions_xyzw,
                len(null_timestamps),
                4,
                "nullspace stored quaternions",
            ),
        },
        "schur": {
            "timestamps_ns": list(schur_timestamps),
            "positions": _flat_rows(
                schur_positions, len(schur_timestamps), 3, "schur positions"
            ),
            "stored_quaternions_xyzw": _flat_rows(
                schur_quaternions_xyzw,
                len(schur_timestamps),
                4,
                "schur stored quaternions",
            ),
        },
        "ground_truth": {
            "timestamps_ns": list(gt_timestamps),
            "positions": _flat_rows(
                ground_truth_positions, len(gt_timestamps), 3, "ground-truth positions"
            ),
            "quaternions_xyzw": _flat_rows(
                ground_truth_quaternions_xyzw,
                len(gt_timestamps),
                4,
                "ground-truth quaternions",
            ),
        },
    }
    payload = _canonical(document)
    decode_sequence_request(payload)
    return payload


def decode_sequence_request(document: Any) -> SequenceMathRequest:
    record = _parse(document, "sequence-math request")
    _exact(
        record,
        (
            "schema_version", "record_type", "sequence_index", "sequence_id",
            "nullspace", "schur", "ground_truth",
        ),
        "sequence-math request",
    )
    if record["schema_version"] != SCHEMA_VERSION or record["record_type"] != REQUEST_RECORD_TYPE:
        _fail("sequence-math request identity differs")
    index = _u64(record["sequence_index"], "sequence index")
    if index >= len(SEQUENCES) or record["sequence_id"] != SEQUENCES[index]:
        _fail("sequence-math request sequence differs from the frozen inventory")
    decoded_modes = {}
    for mode in MODES:
        item = _exact(
            record[mode],
            ("timestamps_ns", "positions", "stored_quaternions_xyzw"),
            mode + " request",
        )
        timestamps = _timestamps(item["timestamps_ns"], mode, unique=True)
        decoded_modes[mode] = (
            timestamps,
            _array_record(item["positions"], (len(timestamps), 3), mode + " positions"),
            _array_record(
                item["stored_quaternions_xyzw"],
                (len(timestamps), 4),
                mode + " stored quaternions",
            ),
        )
    gt = _exact(
        record["ground_truth"],
        ("timestamps_ns", "positions", "quaternions_xyzw"),
        "ground-truth request",
    )
    gt_timestamps = _timestamps(gt["timestamps_ns"], "ground truth", unique=False)
    canonical = bytes(document)
    return SequenceMathRequest(
        index,
        record["sequence_id"],
        decoded_modes["nullspace"][0],
        decoded_modes["nullspace"][1],
        decoded_modes["nullspace"][2],
        decoded_modes["schur"][0],
        decoded_modes["schur"][1],
        decoded_modes["schur"][2],
        gt_timestamps,
        _array_record(gt["positions"], (len(gt_timestamps), 3), "ground-truth positions"),
        _array_record(
            gt["quaternions_xyzw"],
            (len(gt_timestamps), 4),
            "ground-truth quaternions",
        ),
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


def _shared(left: Sequence[int], right: Sequence[int]) -> Tuple[int, ...]:
    result = []
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        if left[left_index] == right[right_index]:
            result.append(left[left_index])
            left_index += 1
            right_index += 1
        elif left[left_index] < right[right_index]:
            left_index += 1
        else:
            right_index += 1
    return tuple(result)


def _nearest_associations(
    timestamps: Sequence[int], ground_truth: Sequence[int]
) -> Tuple[Mapping[str, int], ...]:
    result = []
    for estimator_index, timestamp in enumerate(timestamps):
        if not ground_truth:
            break
        # Independent lower-bound implementation.  Candidate comparison is by
        # (absolute difference, source index), preserving the lower index on a
        # distance tie and selecting the first row of a duplicate lower stamp
        # (the frozen bisect semantics).
        low = 0
        high = len(ground_truth)
        while low < high:
            middle = (low + high) // 2
            if ground_truth[middle] < timestamp:
                low = middle + 1
            else:
                high = middle
        candidates = []
        if low > 0:
            lower_value = ground_truth[low - 1]
            lower_low = 0
            lower_high = low
            while lower_low < lower_high:
                middle = (lower_low + lower_high) // 2
                if ground_truth[middle] < lower_value:
                    lower_low = middle + 1
                else:
                    lower_high = middle
            candidates.append(lower_low)
        if low < len(ground_truth):
            candidates.append(low)
        gt_index = min(
            candidates,
            key=lambda item: (abs(ground_truth[item] - timestamp), item),
        )
        best_delta = abs(ground_truth[gt_index] - timestamp)
        if best_delta <= GROUND_TRUTH_MAX_DIFFERENCE_NS:
            result.append(
                {
                    "estimator_index": estimator_index,
                    "estimator_timestamp_ns": timestamp,
                    "ground_truth_index": gt_index,
                    "ground_truth_timestamp_ns": ground_truth[gt_index],
                    "absolute_difference_ns": best_delta,
                }
            )
    return tuple(result)


def decode_sequence_response(
    document: Any, request_document: Any
) -> SequenceMathResponse:
    request = decode_sequence_request(request_document)
    record = _parse(document, "sequence-math response")
    _exact(
        record,
        (
            "schema_version", "record_type", "sequence_index", "sequence_id",
            "request_sha256", "shared_timestamps_ns", "associations", "alignment",
            "ground_truth_associated_positions", "nullspace_aligned_positions",
            "schur_aligned_positions", "nullspace_aligned_quaternions_xyzw",
            "schur_aligned_quaternions_xyzw", "nullspace_aligned_inverse_rotations",
            "schur_aligned_inverse_rotations", "position_differences_m",
            "orientation_differences_deg", "metrics",
        ),
        "sequence-math response",
    )
    if record["schema_version"] != SCHEMA_VERSION or record["record_type"] != RESPONSE_RECORD_TYPE:
        _fail("sequence-math response identity differs")
    if (
        record["sequence_index"] != request.sequence_index
        or record["sequence_id"] != request.sequence_id
        or record["request_sha256"] != request.sha256
    ):
        _fail("sequence-math response does not bind its exact request")
    full_shared = _shared(
        request.nullspace_timestamps_ns, request.schur_timestamps_ns
    )
    expected_associations = _nearest_associations(
        full_shared, request.ground_truth_timestamps_ns
    )
    expected_shared = tuple(
        row["estimator_timestamp_ns"] for row in expected_associations
    )
    shared = _timestamps(record["shared_timestamps_ns"], "shared", unique=True)
    if shared != expected_shared or len(shared) < 3:
        _fail(
            "response shared timestamps are not the exact associated subset "
            "of the complete mode intersection"
        )
    if type(record["associations"]) is not list or len(record["associations"]) != len(shared):
        _fail("response association population differs")
    retained_associations = []
    association_keys = (
        "estimator_index", "estimator_timestamp_ns", "ground_truth_index",
        "ground_truth_timestamp_ns", "absolute_difference_ns",
    )
    for index, item in enumerate(record["associations"]):
        row = _exact(item, association_keys, "response association")
        retained = {key: _u64(row[key], "association " + key) for key in association_keys}
        if retained != expected_associations[index]:
            _fail("response association differs from independent nearest-row replay")
        retained_associations.append(retained)

    count = len(shared)
    alignment = _exact(
        record["alignment"],
        (
            "rotation", "translation", "quaternion_xyzw", "source_count", "source_singular_values",
            "source_rank_threshold_bits", "cross_covariance_singular_values",
            "cross_covariance_rank_threshold_bits", "reflection_correction_applied",
            "determinant_bits",
            "orthogonality_error_frobenius_bits", "applied_identically_to_both_modes",
        ),
        "response alignment",
    )
    if alignment["source_count"] != count or alignment["applied_identically_to_both_modes"] is not True:
        _fail("response alignment population/reuse marker differs")
    if type(alignment["reflection_correction_applied"]) is not bool:
        _fail("response alignment reflection-correction marker is not Boolean")
    alignment_arrays = {
        "rotation": _array_record(alignment["rotation"], (3, 3), "alignment rotation"),
        "translation": _array_record(alignment["translation"], (3,), "alignment translation"),
        "quaternion_xyzw": _array_record(
            alignment["quaternion_xyzw"], (4,), "alignment quaternion"
        ),
        "source_singular_values": _array_record(
            alignment["source_singular_values"], (3,), "alignment singular values"
        ),
        "cross_covariance_singular_values": _array_record(
            alignment["cross_covariance_singular_values"],
            (3,),
            "alignment cross-covariance singular values",
        ),
    }
    alignment_scalars = {}
    for key in (
        "source_rank_threshold_bits", "cross_covariance_rank_threshold_bits",
        "determinant_bits",
        "orthogonality_error_frobenius_bits",
    ):
        bits_to_f64(alignment[key], "alignment " + key)
        alignment_scalars[key] = alignment[key]
    arrays = {
        "ground_truth_associated_positions": _array_record(
            record["ground_truth_associated_positions"], (count, 3), "associated ground truth"
        ),
        "nullspace_aligned_positions": _array_record(
            record["nullspace_aligned_positions"], (count, 3), "aligned nullspace positions"
        ),
        "schur_aligned_positions": _array_record(
            record["schur_aligned_positions"], (count, 3), "aligned schur positions"
        ),
        "nullspace_aligned_quaternions_xyzw": _array_record(
            record["nullspace_aligned_quaternions_xyzw"],
            (count, 4),
            "aligned nullspace quaternions",
        ),
        "schur_aligned_quaternions_xyzw": _array_record(
            record["schur_aligned_quaternions_xyzw"],
            (count, 4),
            "aligned schur quaternions",
        ),
        "nullspace_aligned_inverse_rotations": _array_record(
            record["nullspace_aligned_inverse_rotations"],
            (count, 3, 3),
            "aligned nullspace inverse rotations",
        ),
        "schur_aligned_inverse_rotations": _array_record(
            record["schur_aligned_inverse_rotations"],
            (count, 3, 3),
            "aligned schur inverse rotations",
        ),
        "position_differences_m": _array_record(
            record["position_differences_m"], (count,), "position differences"
        ),
        "orientation_differences_deg": _array_record(
            record["orientation_differences_deg"], (count,), "orientation differences"
        ),
    }
    expected_gt_bits = []
    for association in retained_associations:
        start = association["ground_truth_index"] * 3
        expected_gt_bits.extend(request.ground_truth_positions.bits[start : start + 3])
    if arrays["ground_truth_associated_positions"].bits != tuple(expected_gt_bits):
        _fail("response associated ground-truth positions differ from request bits")

    metrics = _exact(
        record["metrics"],
        (
            "position_p95_m_bits", "orientation_p95_deg_bits",
            "ate_nullspace_m_bits", "ate_schur_m_bits",
            "relative_ate_difference_bits",
        ),
        "response metrics",
    )
    metric_bits = dict(metrics)
    decoded_metrics = {key: bits_to_f64(value, "metric " + key) for key, value in metrics.items()}
    if any(value < 0.0 for value in decoded_metrics.values()):
        _fail("response metric is negative")
    if decoded_metrics["ate_nullspace_m_bits"] <= 0.0:
        _fail("response baseline ATE is not strictly positive")
    if decoded_metrics["position_p95_m_bits"] > 0.01:
        _fail("response position p95 exceeds its inclusive limit")
    if decoded_metrics["orientation_p95_deg_bits"] > 0.05:
        _fail("response orientation p95 exceeds its inclusive limit")
    if decoded_metrics["relative_ate_difference_bits"] > 0.01:
        _fail("response relative ATE exceeds its inclusive limit")
    for name in ("position_differences_m", "orientation_differences_deg"):
        if any(bits_to_f64(bits, name) < 0.0 for bits in arrays[name].bits):
            _fail(name + " contains a negative value")
    canonical = bytes(document)
    return SequenceMathResponse(
        request.sequence_index,
        request.sequence_id,
        request.sha256,
        shared,
        tuple(retained_associations),
        arrays,
        alignment_arrays,
        alignment_scalars,
        alignment["reflection_correction_applied"],
        metric_bits,
        canonical,
        hashlib.sha256(canonical).hexdigest(),
    )


__all__ = [
    "F64BitsArray",
    "MAX_DOCUMENT_BYTES",
    "QUATERNION_NORM_TOLERANCE",
    "REQUEST_RECORD_TYPE",
    "RESPONSE_RECORD_TYPE",
    "SequenceMathCodecError",
    "SequenceMathRequest",
    "SequenceMathResponse",
    "bits_to_f64",
    "decode_sequence_request",
    "decode_sequence_response",
    "encode_sequence_request",
    "f64_to_bits",
    "validate_stored_quaternion",
]
