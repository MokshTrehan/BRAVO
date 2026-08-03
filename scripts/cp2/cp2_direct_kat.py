#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exact, data-free CP2-D direct-math known-answer bundle boundary.

This module is a proposed, non-authorizing codec/validator.  It performs no
filesystem access, package discovery, NumPy import, subprocess execution, or
recorded-input access.  In particular, it does not invent the stack-specific
SVD/Kabsch/matrix-product answer bits that must come from a reviewed numerical
capsule.  It only makes the transport around those bits closed and testable.

The expectation document fixes the five contract cases, every input row,
output name and shape, and ``repeat_count=2``.  The response must carry the
same request projection in both complete repeats, in the same order, and every
observed finite-binary64 bit string must equal the reviewed expectation.  Case
3 also carries the case-1 alignment bytes so the validator can prove
byte-for-byte reuse.  The three stack-independent answer bits already frozen
in the clarification are enforced here directly.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Dict, Mapping, Sequence, Tuple

import cp2_f64_codec as f64_codec


SCHEMA_VERSION = 1
CODEC = "cp2_f64_known_answer_bundle_v1"
EXPECTATION_RECORD_TYPE = "cp2_d_direct_kat_expectation"
RESPONSE_RECORD_TYPE = "cp2_d_direct_kat_response"
REPEAT_COUNT = 2
MAX_DOCUMENT_BYTES = 4 << 20

CASE_ORDER = (
    "kabsch_proper_full_rank",
    "kabsch_reflection_correction",
    "common_alignment_matrix_products",
    "ordered_translation_rmse",
    "linear_p95_boundary",
)

# The request-side projection is as closed as the response.  All rows are
# flattened in C order and encoded as binary64 bits; no JSON floating-point
# spelling or ambient conversion participates in the retained request.
INPUT_SPECS = (
    (("source_positions", (6, 3)), ("target_positions", (6, 3))),
    (("source_positions", (6, 3)), ("target_positions", (6, 3))),
    (
        ("nullspace_source_positions", (6, 3)),
        ("schur_position_delta", (3,)),
        ("schur_stored_quaternion_xyzw", (4,)),
    ),
    (("aligned_positions", (3, 3)), ("ground_truth_positions", (3, 3))),
    (
        ("boundary_values", (2,)),
        ("quantile", ()),
        ("unsorted_companion_values", (5,)),
    ),
)

_SOURCE_BITS = (
    "4008000000000000", "0000000000000000", "0000000000000000",
    "c008000000000000", "0000000000000000", "0000000000000000",
    "0000000000000000", "4000000000000000", "0000000000000000",
    "0000000000000000", "c000000000000000", "0000000000000000",
    "0000000000000000", "0000000000000000", "3ff0000000000000",
    "0000000000000000", "0000000000000000", "bff0000000000000",
)

FROZEN_INPUT_BITS = (
    (
        _SOURCE_BITS,
        (
            "3ff0000000000000", "3ff0000000000000", "3fe0000000000000",
            "3ff0000000000000", "c014000000000000", "3fe0000000000000",
            "bff0000000000000", "c000000000000000", "3fe0000000000000",
            "4008000000000000", "c000000000000000", "3fe0000000000000",
            "3ff0000000000000", "c000000000000000", "3ff8000000000000",
            "3ff0000000000000", "c000000000000000", "bfe0000000000000",
        ),
    ),
    (
        _SOURCE_BITS,
        (
            "c010000000000000", "4000000000000000", "bfe0000000000000",
            "4000000000000000", "4000000000000000", "bfe0000000000000",
            "bff0000000000000", "4010000000000000", "bfe0000000000000",
            "bff0000000000000", "0000000000000000", "bfe0000000000000",
            "bff0000000000000", "4000000000000000", "3fe0000000000000",
            "bff0000000000000", "4000000000000000", "bff8000000000000",
        ),
    ),
    (
        _SOURCE_BITS,
        ("3fc0000000000000", "bfd0000000000000", "3fe0000000000000"),
        (
            "0000000000000000", "0000000000000000",
            "3fe3333333333333", "3fe999999999999a",
        ),
    ),
    (
        (
            "41a0000000000000", "3ff0000000000000", "3ff0000000000000",
            "3ff0000000000000", "3ff0000000000000", "3ff0000000000000",
            "3ff0000000000000", "3ff0000000000000", "3ff0000000000000",
        ),
        ("0000000000000000",) * 9,
    ),
    (
        ("bff539d94973bf31", "3ff1c926addad2ec"),
        ("3fee666666666666",),
        (
            "4022000000000000", "3ff0000000000000", "401c000000000000",
            "4008000000000000", "4014000000000000",
        ),
    ),
)

# Tuples preserve the only accepted output order.  An empty shape is one
# scalar, matching the canonical finite-binary64 array codec's rank-zero rule.
OUTPUT_SPECS = (
    (
        ("rotation_row_major", (3, 3)),
        ("translation", (3,)),
        ("source_singular_values", (3,)),
        ("source_rank_threshold", ()),
        ("determinant", ()),
        ("orthogonality_error_frobenius", ()),
    ),
    (
        ("rotation_row_major", (3, 3)),
        ("translation", (3,)),
        ("source_singular_values", (3,)),
        ("source_rank_threshold", ()),
        ("determinant", ()),
        ("orthogonality_error_frobenius", ()),
    ),
    (
        ("alignment_rotation_row_major", (3, 3)),
        ("alignment_translation", (3,)),
        ("nullspace_aligned_positions", (6, 3)),
        ("schur_aligned_positions", (6, 3)),
        ("nullspace_aligned_inverse_rotations", (6, 3, 3)),
        ("schur_aligned_inverse_rotations", (6, 3, 3)),
        ("matmul_rounding_discriminator", (3, 3)),
    ),
    (("translation_rmse", ()),),
    (("boundary_p95", ()), ("unsorted_companion_p95", ())),
)

FROZEN_OUTPUT_BITS = {
    (3, "translation_rmse"): "419279a74590331c",
    (4, "boundary_p95"): "3fefab9a2960fd9e",
    (4, "unsorted_companion_p95"): "4021333333333333",
}


class DirectKatError(ValueError):
    """Raised for a malformed, incomplete, or numerically different bundle."""


def _fail(message: str) -> None:
    raise DirectKatError(message)


def _exact_object(value: Any, keys: Sequence[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        _fail(label + " must be a plain JSON object")
    missing = sorted(set(keys) - set(value))
    extra = sorted(set(value) - set(keys))
    if missing or extra or len(value) != len(keys):
        _fail("{} key inventory differs (missing={!r}, extra={!r})".format(label, missing, extra))
    return value


def _reject_float(token: str) -> None:
    _fail("JSON floating-point numbers are forbidden: " + token)


def _reject_constant(token: str) -> None:
    _fail("non-JSON numeric constants are forbidden: " + token)


def _object_without_duplicates(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON object key: " + key)
        result[key] = value
    return result


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
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
        raise DirectKatError("direct-KAT document is not canonical JSON data") from exc


def _parse_document(document: Any, label: str) -> Mapping[str, Any]:
    if type(document) is not bytes:
        _fail(label + " must be immutable bytes")
    if not document or len(document) > MAX_DOCUMENT_BYTES:
        _fail(label + " size is outside the frozen bound")
    if document.startswith(b"\xef\xbb\xbf"):
        _fail(label + " must not contain a UTF-8 BOM")
    try:
        text = document.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise DirectKatError(label + " is not strict UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except DirectKatError:
        raise
    except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as exc:
        raise DirectKatError(label + " is invalid JSON") from exc
    if type(value) is not dict:
        _fail(label + " must contain one top-level object")
    if document != _canonical_bytes(value):
        _fail(label + " is not the unique canonical encoding")
    return value


@dataclass(frozen=True)
class KatOutput:
    name: str
    shape: Tuple[int, ...]
    bits: Tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            _fail("direct-KAT output name is invalid")
        try:
            array = f64_codec.F64Array(shape=self.shape, bits=self.bits)
        except f64_codec.F64CodecError as exc:
            raise DirectKatError("direct-KAT output {}: {}".format(self.name, exc)) from exc
        object.__setattr__(self, "shape", array.shape)
        object.__setattr__(self, "bits", array.bits)


@dataclass(frozen=True)
class KatCase:
    case_index: int
    name: str
    inputs: Tuple[KatOutput, ...]
    outputs: Tuple[KatOutput, ...]


@dataclass(frozen=True)
class DirectKatExpectation:
    cases: Tuple[KatCase, ...]
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class DirectKatResponse:
    repeats: Tuple[Tuple[KatCase, ...], ...]
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class DirectKatValidation:
    expectation_sha256: str
    response_sha256: str
    case_count: int
    repeat_count: int
    passed: bool = True


def _u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > (1 << 64) - 1:
        _fail(label + " must be an unsigned 64-bit integer")
    return value


def _parse_array(value: Any, expected: Tuple[str, Tuple[int, ...]], label: str) -> KatOutput:
    record = _exact_object(value, ("name", "shape", "bits"), label)
    expected_name, expected_shape = expected
    if record["name"] != expected_name:
        _fail(label + " name/order differs from the frozen array surface")
    if type(record["shape"]) is not list:
        _fail(label + " shape must be a JSON array")
    if type(record["bits"]) is not list:
        _fail(label + " bits must be a JSON array")
    shape = tuple(_u64(item, label + " shape dimension") for item in record["shape"])
    if shape != expected_shape:
        _fail(label + " shape differs from the frozen array shape")
    if any(type(item) is not str for item in record["bits"]):
        _fail(label + " contains a non-string binary64 bit value")
    return KatOutput(expected_name, shape, tuple(record["bits"]))


def _parse_cases(value: Any, label: str) -> Tuple[KatCase, ...]:
    if type(value) is not list or len(value) != len(CASE_ORDER):
        _fail(label + " must contain the exact five-case population")
    cases = []
    for case_index, (item, expected_name, input_specs, output_specs) in enumerate(
        zip(value, CASE_ORDER, INPUT_SPECS, OUTPUT_SPECS)
    ):
        record = _exact_object(
            item, ("case_index", "name", "inputs", "outputs"), label + " case"
        )
        if _u64(record["case_index"], label + " case index") != case_index:
            _fail(label + " case indices are not contiguous/in order")
        if record["name"] != expected_name:
            _fail(label + " case name/order differs")
        if type(record["inputs"]) is not list or len(record["inputs"]) != len(input_specs):
            _fail(label + " case input population differs")
        inputs = tuple(
            _parse_array(item_value, spec, "{} case {} input {}".format(label, case_index, index))
            for index, (item_value, spec) in enumerate(zip(record["inputs"], input_specs))
        )
        if tuple(item.bits for item in inputs) != FROZEN_INPUT_BITS[case_index]:
            _fail(label + " case input bits differ from the frozen request")
        if type(record["outputs"]) is not list or len(record["outputs"]) != len(output_specs):
            _fail(label + " case output population differs")
        outputs = tuple(
            _parse_array(output, spec, "{} case {} output {}".format(label, case_index, index))
            for index, (output, spec) in enumerate(zip(record["outputs"], output_specs))
        )
        cases.append(KatCase(case_index, expected_name, inputs, outputs))
    return tuple(cases)


def _output_by_name(case: KatCase, name: str) -> KatOutput:
    for output in case.outputs:
        if output.name == name:
            return output
    _fail("direct-KAT case lacks output " + name)


def _validate_expectation_math(cases: Tuple[KatCase, ...]) -> None:
    for (case_index, output_name), expected_bits in FROZEN_OUTPUT_BITS.items():
        output = _output_by_name(cases[case_index], output_name)
        if output.shape != () or output.bits != (expected_bits,):
            _fail(
                "{} {} differs from its frozen binary64 answer".format(
                    cases[case_index].name, output_name
                )
            )

    # The common-alignment case must retain the exact case-1 alignment bytes;
    # a separately recomputed or one-ULP-different candidate transform rejects.
    if _output_by_name(cases[2], "alignment_rotation_row_major").bits != _output_by_name(
        cases[0], "rotation_row_major"
    ).bits:
        _fail("common-alignment rotation is not byte-identical to case 1")
    if _output_by_name(cases[2], "alignment_translation").bits != _output_by_name(
        cases[0], "translation"
    ).bits:
        _fail("common-alignment translation is not byte-identical to case 1")


def decode_expectation(document: Any) -> DirectKatExpectation:
    """Decode one reviewed known-answer member without deriving answer bits."""

    record = _parse_document(document, "direct-KAT expectation")
    _exact_object(
        record,
        (
            "schema_version",
            "record_type",
            "codec",
            "repeat_count",
            "case_order",
            "cases",
        ),
        "direct-KAT expectation",
    )
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        _fail("direct-KAT expectation schema version differs")
    if record["record_type"] != EXPECTATION_RECORD_TYPE or record["codec"] != CODEC:
        _fail("direct-KAT expectation identity differs")
    if type(record["repeat_count"]) is not int or record["repeat_count"] != REPEAT_COUNT:
        _fail("direct-KAT expectation repeat count must be integer two")
    if record["case_order"] != list(CASE_ORDER):
        _fail("direct-KAT expectation case-order projection differs")
    cases = _parse_cases(record["cases"], "direct-KAT expectation")
    _validate_expectation_math(cases)
    canonical = bytes(document)
    return DirectKatExpectation(cases, canonical, hashlib.sha256(canonical).hexdigest())


def decode_response(document: Any) -> DirectKatResponse:
    """Decode a response structurally; use :func:`validate_pair` for answers."""

    record = _parse_document(document, "direct-KAT response")
    _exact_object(
        record,
        (
            "schema_version",
            "record_type",
            "codec",
            "repeat_count",
            "case_order",
            "repeats",
        ),
        "direct-KAT response",
    )
    if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
        _fail("direct-KAT response schema version differs")
    if record["record_type"] != RESPONSE_RECORD_TYPE or record["codec"] != CODEC:
        _fail("direct-KAT response identity differs")
    if type(record["repeat_count"]) is not int or record["repeat_count"] != REPEAT_COUNT:
        _fail("direct-KAT response repeat count must be integer two")
    if record["case_order"] != list(CASE_ORDER):
        _fail("direct-KAT response case-order projection differs")
    if type(record["repeats"]) is not list or len(record["repeats"]) != REPEAT_COUNT:
        _fail("direct-KAT response must contain exactly two repeats")
    repeats = []
    for repeat_index, item in enumerate(record["repeats"]):
        repeat = _exact_object(item, ("repeat_index", "cases"), "direct-KAT repeat")
        if _u64(repeat["repeat_index"], "direct-KAT repeat index") != repeat_index:
            _fail("direct-KAT repeat indices are not contiguous/in order")
        repeats.append(_parse_cases(repeat["cases"], "direct-KAT repeat {}".format(repeat_index)))
    canonical = bytes(document)
    return DirectKatResponse(tuple(repeats), canonical, hashlib.sha256(canonical).hexdigest())


def _case_bits(cases: Tuple[KatCase, ...]) -> Tuple[Tuple[Tuple[str, ...], ...], ...]:
    return tuple(tuple(output.bits for output in case.outputs) for case in cases)


def _case_input_bits(cases: Tuple[KatCase, ...]) -> Tuple[Tuple[Tuple[str, ...], ...], ...]:
    return tuple(tuple(item.bits for item in case.inputs) for case in cases)


def validate_pair(expectation_document: Any, response_document: Any) -> DirectKatValidation:
    """Require two observed repetitions to equal one reviewed expectation."""

    expectation = decode_expectation(expectation_document)
    response = decode_response(response_document)
    expected = _case_bits(expectation.cases)
    expected_inputs = _case_input_bits(expectation.cases)
    for repeat_index, cases in enumerate(response.repeats):
        if _case_input_bits(cases) != expected_inputs:
            _fail("direct-KAT repeat {} differs from the frozen request bits".format(repeat_index))
        if _case_bits(cases) != expected:
            _fail("direct-KAT repeat {} differs from reviewed answer bits".format(repeat_index))
    if _case_bits(response.repeats[0]) != _case_bits(response.repeats[1]):
        _fail("direct-KAT repeats are not bit-identical")
    return DirectKatValidation(
        expectation.sha256,
        response.sha256,
        len(expectation.cases),
        len(response.repeats),
        True,
    )


__all__ = [
    "CASE_ORDER",
    "CODEC",
    "DirectKatError",
    "DirectKatExpectation",
    "DirectKatResponse",
    "DirectKatValidation",
    "EXPECTATION_RECORD_TYPE",
    "FROZEN_INPUT_BITS",
    "FROZEN_OUTPUT_BITS",
    "INPUT_SPECS",
    "OUTPUT_SPECS",
    "REPEAT_COUNT",
    "RESPONSE_RECORD_TYPE",
    "decode_expectation",
    "decode_response",
    "validate_pair",
]
