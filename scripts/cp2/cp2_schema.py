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

SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

ACCEPTED_SET_DOMAIN = b"SchurVIO-CP2-accepted-set-v1\0"
ACCEPTED_SEQUENCE_DOMAIN = b"SchurVIO-CP2-accepted-sequence-v1\0"
COMMAND_ENVIRONMENT_DOMAIN = b"SchurVIO-CP2-command-environment-v1\0"
RESOLVED_PARAMETERS_DOMAIN = b"SchurVIO-CP2-ros-params-v1\0"
RESOLVED_PARAMETER_PREFIX = "/cp2_vio/"

ALLOWED_COMMAND_ENVIRONMENT_NAMES = frozenset(
    {
        "CC",
        "CFLAGS",
        "CMAKE_PREFIX_PATH",
        "CP2_FORBID_BAG_ACCESS",
        "CP2_SELF_TEST",
        "CPATH",
        "CXX",
        "CXXFLAGS",
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "LDFLAGS",
        "LIBRARY_PATH",
        "LOGNAME",
        "OMP_NUM_THREADS",
        "PATH",
        "PKG_CONFIG_PATH",
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
        "USER",
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
    "I64_MAX",
    "I64_MIN",
    "RESOLVED_PARAMETERS_DOMAIN",
    "SAFE_ID_PATTERN",
    "SchemaError",
    "SHA256_PATTERN",
    "U64_MAX",
    "accepted_sequence_sha256",
    "accepted_set_sha256",
    "command_environment_sha256",
    "encode_accepted_sequence",
    "encode_accepted_set",
    "encode_command_environment",
    "encode_parameter_value",
    "encode_resolved_parameters",
    "json_line_bytes",
    "jsonl_bytes",
    "resolved_parameters_sha256",
    "sha256_file",
    "sha256_stream",
    "strict_json_load",
    "strict_json_loads",
    "typed_parameter_value",
    "typed_resolved_parameters",
    "validate_f64",
    "validate_i64",
    "validate_relpath",
    "validate_safe_id",
    "validate_sha256",
    "validate_u64",
]
