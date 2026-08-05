#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pure-stdlib schema primitives for SchurVIO-Lite CP2 evidence.

This module deliberately performs no repository, ROS, dataset, or artifact
access at import time.  It centralizes only byte-level encodings and strict
validators shared by future CP2 runners and verifiers.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import struct
from typing import Any, BinaryIO, Iterable, List, Tuple


U64_MAX = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
EXACT_BINARY64_INTEGER_MAX = (1 << 53) - 1

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_LINE_PATTERN = re.compile(rb"^([0-9a-f]{64})  ([^\r\n]+)\n$")

ACCEPTED_SET_DOMAIN = b"SchurVIO-CP2-accepted-set-v1\0"
ACCEPTED_SEQUENCE_DOMAIN = b"SchurVIO-CP2-accepted-sequence-v1\0"
COMMAND_ENVIRONMENT_DOMAIN = b"SchurVIO-CP2-command-environment-v1\0"
RESOLVED_PARAMETERS_DOMAIN = b"SchurVIO-CP2-ros-params-v1\0"
RESOLVED_PARAMETER_PREFIX = "/cp2_vio/"

ALLOWED_COMMAND_ENVIRONMENT_NAMES = frozenset(
    {
        "CC",
        "BLIS_NUM_THREADS",
        "CFLAGS",
        "CMAKE_PREFIX_PATH",
        "CP2_FORBID_BAG_ACCESS",
        "CP2_POSTAUTH_PAIR_INDEX",
        "CP2_SELF_TEST",
        "CPATH",
        "CXX",
        "CXXFLAGS",
        "GIT_ALLOW_PROTOCOL",
        "GIT_ATTR_NOSYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_LITERAL_PATHSPECS",
        "GIT_NO_LAZY_FETCH",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OPTIONAL_LOCKS",
        "GIT_PAGER",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_TERMINAL_PROMPT",
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "LDFLAGS",
        "LIBRARY_PATH",
        "LOGNAME",
        "MKL_DYNAMIC",
        "MKL_NUM_THREADS",
        "MPLCONFIGDIR",
        "NPY_DISABLE_CPU_FEATURES",
        "NUMEXPR_NUM_THREADS",
        "OMP_DYNAMIC",
        "OMP_NUM_THREADS",
        "OPENBLAS_CORETYPE",
        "OPENBLAS_NUM_THREADS",
        "PATH",
        "PKG_CONFIG_PATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "ROS_DISTRO",
        "ROS_ETC_DIR",
        "ROS_HOSTNAME",
        "ROS_IP",
        "ROS_MASTER_URI",
        "ROS_PACKAGE_PATH",
        "ROS_PYTHON_VERSION",
        "ROS_ROOT",
        "ROS_VERSION",
        "SOURCE_DATE_EPOCH",
        "TMPDIR",
        "TZ",
        "USER",
        "VECLIB_MAXIMUM_THREADS",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    }
)


class SchemaError(ValueError):
    """Raised when a value cannot satisfy the frozen CP2 schema."""


def _fail(message: str) -> None:
    raise SchemaError(message)


def _utf8(value: Any, label: str, forbid_nul: bool = False) -> bytes:
    if not isinstance(value, str):
        _fail("{} must be a string".format(label))
    if forbid_nul and "\0" in value:
        _fail("{} must not contain NUL".format(label))
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise SchemaError("{} is not valid UTF-8: {}".format(label, exc)) from exc


def _u64_bytes(value: Any, label: str) -> bytes:
    return validate_u64(value, label).to_bytes(8, "big", signed=False)


def _i64_bytes(value: Any, label: str) -> bytes:
    return validate_i64(value, label).to_bytes(8, "big", signed=True)


def _length_prefixed(encoded: bytes, label: str) -> bytes:
    return _u64_bytes(len(encoded), label + " byte length") + encoded


def _length_prefixed_utf8(value: Any, label: str, forbid_nul: bool = False) -> bytes:
    return _length_prefixed(_utf8(value, label, forbid_nul=forbid_nul), label)


def _reject_json_constant(token: str) -> None:
    _fail("non-JSON numeric constant is forbidden: " + token)


def _parse_json_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        _fail("JSON number is not finite binary64: " + token)
    return value


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON object key: " + key)
        result[key] = value
    return result


def _validate_json_tree(value: Any, label: str = "JSON value") -> None:
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            _fail(label + " contains a non-finite number")
        return
    if isinstance(value, str):
        _utf8(value, label)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, "{}[{}]".format(label, index))
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _utf8(key, label + " object key")
            _validate_json_tree(item, "{}[{!r}]".format(label, key))
        return
    _fail("{} contains a non-JSON value of type {}".format(label, type(value).__name__))


def strict_json_loads(document: Any) -> Any:
    """Parse one UTF-8 JSON document, rejecting duplicate keys and nonfinite values."""

    if isinstance(document, (bytes, bytearray)):
        try:
            text = bytes(document).decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise SchemaError("JSON document is not UTF-8: {}".format(exc)) from exc
    elif isinstance(document, str):
        _utf8(document, "JSON document")
        text = document
    else:
        _fail("JSON document must be str, bytes, or bytearray")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except SchemaError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SchemaError("invalid JSON: {}".format(exc)) from exc
    _validate_json_tree(value)
    return value


def strict_json_load(stream: Any) -> Any:
    """Read and strictly parse one JSON document from a file-like object."""

    if not hasattr(stream, "read"):
        _fail("JSON stream must provide read()")
    return strict_json_loads(stream.read())


def strict_jsonl_loads(document: Any) -> List[dict]:
    """Parse deterministic LF-terminated JSONL with no blank physical lines."""

    if isinstance(document, (bytes, bytearray)):
        raw = bytes(document)
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise SchemaError("JSONL document is not UTF-8: {}".format(exc)) from exc
    elif isinstance(document, str):
        _utf8(document, "JSONL document")
        text = document
    else:
        _fail("JSONL document must be str, bytes, or bytearray")
    if "\r" in text:
        _fail("JSONL document must use LF newlines only")
    if text and not text.endswith("\n"):
        _fail("nonempty JSONL document must end with one LF")

    records = []
    for physical_index, line in enumerate(text.splitlines(), 1):
        if not line:
            _fail("JSONL document contains a blank line at {}".format(physical_index))
        value = strict_json_loads(line)
        if not isinstance(value, Mapping):
            _fail("JSONL line {} is not an object".format(physical_index))
        records.append(dict(value))
    return records


def exact_object_keys(value: Any, expected_keys: Iterable[str], label: str = "object") -> Mapping:
    """Require one mapping to have exactly the declared string-key inventory."""

    if not isinstance(value, Mapping):
        _fail(label + " must be an object")
    try:
        expected = list(expected_keys)
    except TypeError as exc:
        raise SchemaError(label + " expected keys must be iterable") from exc
    if any(not isinstance(key, str) for key in expected) or len(set(expected)) != len(expected):
        _fail(label + " expected keys must be unique strings")
    if set(value) != set(expected):
        missing = sorted(set(expected) - set(value))
        extra = sorted(set(value) - set(expected))
        _fail("{} key inventory differs (missing={!r}, extra={!r})".format(label, missing, extra))
    return value


def json_line_bytes(record: Any) -> bytes:
    """Emit one deterministic compact UTF-8 JSON object plus one newline."""

    if not isinstance(record, Mapping):
        _fail("JSONL record must be an object")
    _validate_json_tree(record, "JSONL record")
    try:
        text = json.dumps(
            record,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (text + "\n").encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SchemaError("cannot emit JSONL record: {}".format(exc)) from exc


def jsonl_bytes(records: Iterable[Any]) -> bytes:
    """Emit zero or more compact JSON object lines without blank lines."""

    if isinstance(records, (str, bytes, bytearray)):
        _fail("JSONL records must be an iterable of objects")
    return b"".join(json_line_bytes(record) for record in records)


def validate_safe_id(value: Any, label: str = "SAFE_ID") -> str:
    if not isinstance(value, str) or SAFE_ID_PATTERN.fullmatch(value) is None:
        _fail("{} does not match [A-Za-z0-9][A-Za-z0-9._-]{{0,127}}".format(label))
    return value


def validate_relpath(value: Any, label: str = "relpath") -> str:
    encoded = _utf8(value, label, forbid_nul=True)
    del encoded
    if not value or "\\" in value:
        _fail("{} must be a nonempty POSIX relative path".format(label))
    pure = PurePosixPath(value)
    if not pure.parts or pure.is_absolute() or str(pure) != value:
        _fail("{} must be normalized and relative".format(label))
    if any(part in ("", ".", "..") for part in pure.parts):
        _fail("{} contains a forbidden path component".format(label))
    return value


def validate_sha256(value: Any, label: str = "sha256") -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        _fail("{} must be 64 lowercase hexadecimal characters".format(label))
    return value


def validate_u64(value: Any, label: str = "u64") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        _fail("{} must be an integer in [0,2^64-1] and not Boolean".format(label))
    return value


def validate_i64(value: Any, label: str = "i64") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not I64_MIN <= value <= I64_MAX:
        _fail("{} must be an integer in [-2^63,2^63-1] and not Boolean".format(label))
    return value


def validate_f64(value: Any, label: str = "f64") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("{} must be a JSON number and not Boolean".format(label))
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise SchemaError("{} is not representable as binary64".format(label)) from exc
    if not math.isfinite(converted):
        _fail("{} must be finite binary64".format(label))
    return converted


def checked_u64_add(left: Any, right: Any, label: str = "u64 sum") -> int:
    """Add two u64 values without wraparound, saturation, or clamping."""

    lhs = validate_u64(left, label + " left operand")
    rhs = validate_u64(right, label + " right operand")
    if lhs > U64_MAX - rhs:
        _fail(label + " overflows u64")
    return lhs + rhs


def checked_u64_multiply(left: Any, right: Any, label: str = "u64 product") -> int:
    """Multiply two u64 values without wraparound, saturation, or clamping."""

    lhs = validate_u64(left, label + " left operand")
    rhs = validate_u64(right, label + " right operand")
    if lhs != 0 and rhs > U64_MAX // lhs:
        _fail(label + " overflows u64")
    return lhs * rhs


def checked_u64_sum(values: Iterable[Any], label: str = "u64 sum") -> int:
    """Accumulate an iterable of u64 values, checking every addition."""

    if isinstance(values, (str, bytes, bytearray)):
        _fail(label + " values must be an iterable of integers")
    total = 0
    try:
        for index, value in enumerate(values):
            total = checked_u64_add(total, value, "{} at index {}".format(label, index))
    except TypeError as exc:
        raise SchemaError(label + " values must be iterable") from exc
    return total


def exact_count_ratio(numerator: Any, denominator: Any, label: str = "count ratio") -> float:
    """Form the frozen one-conversion-per-operand binary64 diagnostic ratio.

    The caller, rather than this rounded value, must use an exact integer
    cross-product for every normative gate.  CP2 rejects operands above 2^53-1
    so their individual conversions to binary64 are exact.
    """

    num = validate_u64(numerator, label + " numerator")
    den = validate_u64(denominator, label + " denominator")
    if den == 0:
        _fail(label + " denominator must be nonzero")
    if num > EXACT_BINARY64_INTEGER_MAX or den > EXACT_BINARY64_INTEGER_MAX:
        _fail(label + " operand exceeds the exact binary64 integer range")
    ratio = float(num) / float(den)
    if not math.isfinite(ratio):
        _fail(label + " is not finite")
    return ratio


def agreement_gate_999_per_1000(numerator: Any, denominator: Any) -> bool:
    """Apply CP2's normative 99.9% gate using exact wide integer products."""

    num = validate_u64(numerator, "agreement numerator")
    den = validate_u64(denominator, "agreement denominator")
    if den == 0 or num > den:
        return False
    # Python integers are arbitrary precision, so neither side can wrap.  This
    # deliberately does not reuse the rounded diagnostic ratio.
    return 1000 * num >= 999 * den


def decimal_seconds_to_ns(value: Any, label: str = "decimal timestamp") -> int:
    """Parse a nonnegative decimal timestamp exactly into integer nanoseconds."""

    if not isinstance(value, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]{1,9})?", value) is None:
        _fail(label + " must be nonnegative decimal seconds with at most nine fractional digits")
    whole_text, separator, fractional_text = value.partition(".")
    whole = int(whole_text, 10)
    fractional = int((fractional_text if separator else "").ljust(9, "0") or "0", 10)
    whole_ns = checked_u64_multiply(whole, 1_000_000_000, label + " whole seconds")
    return checked_u64_add(whole_ns, fractional, label)


def linear_quantile_integer_ns(values: Iterable[Any], q: Any, label: str = "linear quantile") -> float:
    """Compute the frozen numpy-linear quantile over sorted integer nanoseconds."""

    if isinstance(values, (str, bytes, bytearray)):
        _fail(label + " values must be an iterable of u64 integers")
    try:
        ordered = [validate_u64(value, label + " sample") for value in values]
    except TypeError as exc:
        raise SchemaError(label + " values must be iterable") from exc
    if not ordered:
        _fail(label + " requires a nonempty population")
    if any(ordered[index] > ordered[index + 1] for index in range(len(ordered) - 1)):
        _fail(label + " population must already be sorted")
    quantile = validate_f64(q, label + " q")
    if quantile < 0.0 or quantile > 1.0:
        _fail(label + " q must be in [0,1]")
    if any(value > EXACT_BINARY64_INTEGER_MAX for value in ordered):
        _fail(label + " sample exceeds the exact binary64 integer range")

    h = float(len(ordered) - 1) * quantile
    lo = int(math.floor(h))
    hi = int(math.ceil(h))
    result = float(ordered[lo]) + (h - float(lo)) * float(ordered[hi] - ordered[lo])
    if not math.isfinite(result):
        _fail(label + " result is not finite")
    return result


def sha256_stream(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> str:
    """Return lowercase SHA-256 for bytes read incrementally from ``stream``."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        _fail("chunk_size must be a positive integer")
    if not hasattr(stream, "read"):
        _fail("SHA-256 stream must provide read()")
    digest = hashlib.sha256()
    while True:
        block = stream.read(chunk_size)
        if block == b"":
            return digest.hexdigest()
        if not isinstance(block, bytes):
            _fail("SHA-256 stream read() must return bytes")
        digest.update(block)


def sha256_file(path: Any, chunk_size: int = 1024 * 1024) -> str:
    """Stream an ordinary SHA-256 over the bytes at ``path``."""

    with Path(path).open("rb") as stream:
        return sha256_stream(stream, chunk_size=chunk_size)


def manifest_bytes(entries: Any) -> bytes:
    """Encode the exact bytewise-sorted CP2 SHA256SUMS record population."""

    if not isinstance(entries, Mapping):
        _fail("manifest entries must be a mapping")
    records = []
    for path, digest in entries.items():
        normalized = validate_relpath(path, "manifest path")
        if normalized == "SHA256SUMS":
            _fail("SHA256SUMS must not list itself")
        digest_value = validate_sha256(digest, "manifest digest for " + normalized)
        encoded_path = _utf8(normalized, "manifest path", forbid_nul=True)
        if b"\n" in encoded_path or b"\r" in encoded_path:
            _fail("manifest path contains a newline")
        records.append((encoded_path, digest_value.encode("ascii")))
    records.sort(key=lambda item: item[0])
    return b"".join(digest + b"  " + path + b"\n" for path, digest in records)


def parse_manifest_bytes(document: Any) -> dict:
    """Parse an exact sorted SHA256SUMS byte stream without path normalization."""

    if not isinstance(document, (bytes, bytearray)):
        _fail("manifest document must be bytes or bytearray")
    raw = bytes(document)
    if raw and not raw.endswith(b"\n"):
        _fail("nonempty manifest must end with LF")
    entries = {}
    previous_path = None
    for line_index, line in enumerate(raw.splitlines(keepends=True), 1):
        match = MANIFEST_LINE_PATTERN.fullmatch(line)
        if match is None:
            _fail("manifest line {} does not have the exact format".format(line_index))
        digest_bytes, path_bytes = match.groups()
        try:
            path = path_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise SchemaError("manifest path is not UTF-8: {}".format(exc)) from exc
        validate_relpath(path, "manifest path")
        if path == "SHA256SUMS":
            _fail("SHA256SUMS must not list itself")
        if previous_path is not None and path_bytes <= previous_path:
            _fail("manifest paths are duplicated or not strictly bytewise sorted")
        previous_path = path_bytes
        entries[path] = digest_bytes.decode("ascii")
    if manifest_bytes(entries) != raw:
        _fail("manifest bytes are not canonical")
    return entries


def _accepted_id_list(feature_ids: Iterable[Any], label: str) -> List[int]:
    if isinstance(feature_ids, (str, bytes, bytearray)):
        _fail(label + " must be an iterable of feature IDs")
    try:
        values = [validate_u64(item, label + " feature ID") for item in feature_ids]
    except TypeError as exc:
        raise SchemaError(label + " must be an iterable of feature IDs") from exc
    if len(set(values)) != len(values):
        _fail(label + " feature IDs must be unique")
    return values


def encode_accepted_set(feature_ids: Iterable[Any]) -> bytes:
    """Encode a unique accepted-feature set in strictly ascending ID order."""

    values = sorted(_accepted_id_list(feature_ids, "accepted set"))
    return ACCEPTED_SET_DOMAIN + _u64_bytes(len(values), "accepted set count") + b"".join(
        _u64_bytes(value, "accepted set feature ID") for value in values
    )


def encode_accepted_sequence(feature_ids: Iterable[Any]) -> bytes:
    """Encode a unique accepted-feature sequence in retained processing order."""

    values = _accepted_id_list(feature_ids, "accepted sequence")
    return ACCEPTED_SEQUENCE_DOMAIN + _u64_bytes(len(values), "accepted sequence count") + b"".join(
        _u64_bytes(value, "accepted sequence feature ID") for value in values
    )


def accepted_set_sha256(feature_ids: Iterable[Any]) -> str:
    return hashlib.sha256(encode_accepted_set(feature_ids)).hexdigest()


def accepted_sequence_sha256(feature_ids: Iterable[Any]) -> str:
    return hashlib.sha256(encode_accepted_sequence(feature_ids)).hexdigest()


def encode_command_environment(variables: Any) -> bytes:
    """Encode one complete, allowlisted top-level command environment."""

    if not isinstance(variables, Mapping):
        _fail("command environment must be a mapping")
    encoded_records = []
    for name, value in variables.items():
        name_bytes = _utf8(name, "environment name", forbid_nul=True)
        if name not in ALLOWED_COMMAND_ENVIRONMENT_NAMES:
            _fail("environment name is not allowlisted: " + name)
        value_bytes = _utf8(value, "environment value for " + name, forbid_nul=True)
        encoded_records.append((name_bytes, value_bytes))
    encoded_records.sort(key=lambda item: item[0])

    payload = bytearray(COMMAND_ENVIRONMENT_DOMAIN)
    payload.extend(_u64_bytes(len(encoded_records), "environment variable count"))
    for name_bytes, value_bytes in encoded_records:
        payload.extend(_length_prefixed(name_bytes, "environment name"))
        payload.extend(_length_prefixed(value_bytes, "environment value"))
    return bytes(payload)


def command_environment_sha256(variables: Any) -> str:
    return hashlib.sha256(encode_command_environment(variables)).hexdigest()


def _sorted_parameter_items(value: Mapping, label: str) -> List[Tuple[bytes, str, Any]]:
    encoded_items = []
    for name, item in value.items():
        name_bytes = _utf8(name, label + " key", forbid_nul=True)
        encoded_items.append((name_bytes, name, item))
    encoded_items.sort(key=lambda item: item[0])
    return encoded_items


def encode_parameter_value(value: Any, label: str = "parameter value") -> bytes:
    """Encode one recursive ROS parameter value using the frozen typed tags."""

    if isinstance(value, bool):
        return b"b" + (b"\x01" if value else b"\x00")
    if isinstance(value, int):
        return b"i" + _i64_bytes(value, label)
    if isinstance(value, float):
        finite = validate_f64(value, label)
        return b"f" + struct.pack(">d", finite)
    if isinstance(value, str):
        return b"s" + _length_prefixed_utf8(value, label, forbid_nul=True)
    if isinstance(value, list):
        payload = bytearray(b"l")
        payload.extend(_u64_bytes(len(value), label + " list count"))
        for index, item in enumerate(value):
            payload.extend(encode_parameter_value(item, "{}[{}]".format(label, index)))
        return bytes(payload)
    if isinstance(value, Mapping):
        items = _sorted_parameter_items(value, label)
        payload = bytearray(b"m")
        payload.extend(_u64_bytes(len(items), label + " map count"))
        for name_bytes, name, item in items:
            payload.extend(_length_prefixed(name_bytes, label + " key"))
            payload.extend(encode_parameter_value(item, "{}[{!r}]".format(label, name)))
        return bytes(payload)
    if value is None:
        _fail(label + " must not be null")
    _fail("{} has forbidden type {}".format(label, type(value).__name__))


def typed_parameter_value(value: Any, label: str = "parameter value") -> dict:
    """Return the exact typed-JSON object corresponding to a parameter value."""

    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": validate_i64(value, label)}
    if isinstance(value, float):
        finite = validate_f64(value, label)
        return {"type": "double", "bits": struct.pack(">d", finite).hex()}
    if isinstance(value, str):
        _utf8(value, label, forbid_nul=True)
        return {"type": "string", "value": value}
    if isinstance(value, list):
        return {
            "type": "list",
            "value": [
                typed_parameter_value(item, "{}[{}]".format(label, index))
                for index, item in enumerate(value)
            ],
        }
    if isinstance(value, Mapping):
        return {
            "type": "map",
            "value": [
                {
                    "name": name,
                    "value": typed_parameter_value(item, "{}[{!r}]".format(label, name)),
                }
                for _, name, item in _sorted_parameter_items(value, label)
            ],
        }
    if value is None:
        _fail(label + " must not be null")
    _fail("{} has forbidden type {}".format(label, type(value).__name__))


def _validate_resolved_parameter_map(parameters: Any) -> Mapping:
    if not isinstance(parameters, Mapping):
        _fail("resolved parameters must be a top-level map")
    for name in parameters:
        _utf8(name, "resolved parameter name", forbid_nul=True)
        if not name.startswith(RESOLVED_PARAMETER_PREFIX) or len(name) == len(RESOLVED_PARAMETER_PREFIX):
            _fail("resolved parameter name must be a leaf below /cp2_vio/: " + name)
    return parameters


def encode_resolved_parameters(parameters: Any) -> bytes:
    """Encode the canonical domain-separated flattened ``/cp2_vio/`` map."""

    validated = _validate_resolved_parameter_map(parameters)
    return RESOLVED_PARAMETERS_DOMAIN + encode_parameter_value(validated, "resolved parameters")


def typed_resolved_parameters(parameters: Any) -> dict:
    """Return the exact typed-JSON map for a flattened resolved-parameter map."""

    validated = _validate_resolved_parameter_map(parameters)
    return typed_parameter_value(validated, "resolved parameters")


def resolved_parameters_sha256(parameters: Any) -> str:
    return hashlib.sha256(encode_resolved_parameters(parameters)).hexdigest()


__all__ = [
    "ACCEPTED_SEQUENCE_DOMAIN",
    "ACCEPTED_SET_DOMAIN",
    "ALLOWED_COMMAND_ENVIRONMENT_NAMES",
    "COMMAND_ENVIRONMENT_DOMAIN",
    "EXACT_BINARY64_INTEGER_MAX",
    "I64_MAX",
    "I64_MIN",
    "MANIFEST_LINE_PATTERN",
    "RESOLVED_PARAMETERS_DOMAIN",
    "SAFE_ID_PATTERN",
    "SchemaError",
    "SHA256_PATTERN",
    "U64_MAX",
    "accepted_sequence_sha256",
    "accepted_set_sha256",
    "agreement_gate_999_per_1000",
    "checked_u64_add",
    "checked_u64_multiply",
    "checked_u64_sum",
    "command_environment_sha256",
    "encode_accepted_sequence",
    "encode_accepted_set",
    "encode_command_environment",
    "encode_parameter_value",
    "encode_resolved_parameters",
    "exact_count_ratio",
    "exact_object_keys",
    "json_line_bytes",
    "jsonl_bytes",
    "linear_quantile_integer_ns",
    "manifest_bytes",
    "parse_manifest_bytes",
    "resolved_parameters_sha256",
    "sha256_file",
    "sha256_stream",
    "strict_json_load",
    "strict_jsonl_loads",
    "strict_json_loads",
    "decimal_seconds_to_ns",
    "typed_parameter_value",
    "typed_resolved_parameters",
    "validate_f64",
    "validate_i64",
    "validate_relpath",
    "validate_safe_id",
    "validate_sha256",
    "validate_u64",
]
