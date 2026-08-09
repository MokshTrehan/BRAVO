#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical finite-binary64 array codec for CP2-D known-answer IPC.

The wire representation is deliberately small and closed::

    {"bits":["3ff0000000000000"],"shape":[1]}\n

``bits`` is the C-row-major flattened payload.  Every element is the exact
big-endian IEEE-754 binary64 bit pattern, written as 16 lowercase hexadecimal
digits.  JSON numbers are used only for unsigned 64-bit shape dimensions;
binary64 values are never rendered as JSON numbers.

Decoding is intentionally stricter than ordinary JSON parsing.  After
duplicate-key and type validation, the document is re-encoded and required to
match byte-for-byte.  This rejects alternate key order, whitespace, escapes,
integer spellings, BOMs, and newline conventions without maintaining a second
notion of "canonical" JSON.

This module is pure standard library and performs no filesystem, environment,
repository, dataset, or artifact access at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
import struct
from typing import Any, List, Mapping, Tuple


U64_MAX = (1 << 64) - 1
MAX_DOCUMENT_BYTES = 1 << 20
MAX_RANK = 8
MAX_ELEMENTS = 32_768
_BITS_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_EXACT_KEYS = frozenset(("bits", "shape"))


class F64CodecError(ValueError):
    """Raised when a value or document violates the canonical codec."""


def _fail(message: str) -> None:
    raise F64CodecError(message)


def _validate_dimension(value: Any, label: str) -> int:
    # bool is an int subclass and must not silently become dimension 0 or 1.
    if type(value) is not int:
        _fail("{} must be an unsigned 64-bit integer".format(label))
    if value < 0 or value > U64_MAX:
        _fail("{} is outside unsigned 64-bit range".format(label))
    return value


def _normalize_shape(shape: Any) -> Tuple[int, ...]:
    if not isinstance(shape, (list, tuple)):
        _fail("shape must be a list or tuple")
    dimensions = tuple(
        _validate_dimension(value, "shape[{}]".format(index))
        for index, value in enumerate(shape)
    )
    if len(dimensions) > MAX_RANK:
        _fail("shape rank exceeds the frozen resource bound")
    return dimensions


def checked_element_count(shape: Any) -> int:
    """Return the row-major element count using checked unsigned-u64 products.

    The multiplication follows shape order.  Every intermediate product must
    fit in u64; no unbounded Python-integer result is silently accepted.
    """

    dimensions = _normalize_shape(shape)
    count = 1
    for index, dimension in enumerate(dimensions):
        if count != 0 and dimension > U64_MAX // count:
            _fail("shape element count overflows unsigned 64-bit at index {}".format(index))
        count *= dimension
    if count > MAX_ELEMENTS:
        _fail("shape element count exceeds the frozen resource bound")
    return count


def f64_to_bits_hex(value: Any) -> str:
    """Encode one finite native Python binary64 as canonical big-endian hex."""

    # Requiring a native float prevents implicit, potentially stateful numeric
    # conversions and makes the binary64 boundary explicit to callers.
    if type(value) is not float:
        _fail("binary64 scalar must be a native Python float")
    if not math.isfinite(value):
        _fail("binary64 scalar must be finite")
    return struct.pack(">d", value).hex()


def bits_hex_to_f64(bits: Any) -> float:
    """Decode one canonical big-endian finite IEEE-754 binary64 bit string."""

    if not isinstance(bits, str) or _BITS_PATTERN.fullmatch(bits) is None:
        _fail("binary64 bits must be exactly 16 lowercase hexadecimal digits")
    value = struct.unpack(">d", bytes.fromhex(bits))[0]
    if not math.isfinite(value):
        _fail("binary64 bits encode a non-finite value")
    return value


def _normalize_bits(bits: Any, expected_count: int) -> Tuple[str, ...]:
    if not isinstance(bits, (list, tuple)):
        _fail("bits must be a list or tuple")
    if len(bits) != expected_count:
        _fail(
            "bits count {} does not match shape element count {}".format(
                len(bits), expected_count
            )
        )
    normalized = []
    for index, item in enumerate(bits):
        try:
            bits_hex_to_f64(item)
        except F64CodecError as exc:
            raise F64CodecError("bits[{}]: {}".format(index, exc)) from exc
        normalized.append(item)
    return tuple(normalized)


def _canonical_bytes_from_bits(shape: Tuple[int, ...], bits: Tuple[str, ...]) -> bytes:
    # sort_keys=True places "bits" before "shape" and fixes that order.
    text = json.dumps(
        {"shape": list(shape), "bits": list(bits)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8", "strict") + b"\n"


def encode_f64_bits_array(shape: Any, bits: Any) -> bytes:
    """Encode an already bit-exact flattened array into canonical JSON bytes."""

    normalized_shape = _normalize_shape(shape)
    count = checked_element_count(normalized_shape)
    normalized_bits = _normalize_bits(bits, count)
    return _canonical_bytes_from_bits(normalized_shape, normalized_bits)


def encode_f64_array(shape: Any, values: Any) -> bytes:
    """Encode flattened C-row-major finite binary64 values as canonical bytes."""

    normalized_shape = _normalize_shape(shape)
    count = checked_element_count(normalized_shape)
    if not isinstance(values, (list, tuple)):
        _fail("values must be a list or tuple in flattened C-row-major order")
    if len(values) != count:
        _fail(
            "values count {} does not match shape element count {}".format(
                len(values), count
            )
        )
    bits = []
    for index, value in enumerate(values):
        try:
            bits.append(f64_to_bits_hex(value))
        except F64CodecError as exc:
            raise F64CodecError("values[{}]: {}".format(index, exc)) from exc
    return _canonical_bytes_from_bits(normalized_shape, tuple(bits))


def _reject_float(token: str) -> None:
    _fail("JSON floating-point numbers are forbidden: " + token)


def _reject_constant(token: str) -> None:
    _fail("non-JSON numeric constants are forbidden: " + token)


def _object_without_duplicates(pairs: List[Tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON object key: " + key)
        result[key] = value
    return result


def _parse_document(document: Any) -> Tuple[bytes, Mapping[str, Any]]:
    if type(document) not in (bytes, bytearray):
        _fail("document must be UTF-8 bytes or bytearray")
    if len(document) == 0 or len(document) > MAX_DOCUMENT_BYTES:
        _fail("document size is outside the frozen resource bound")
    raw = bytes(document)
    if raw.startswith(b"\xef\xbb\xbf"):
        _fail("UTF-8 BOM is forbidden")
    if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        _fail("document must have exactly one terminal LF")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise F64CodecError("document is not strict UTF-8: {}".format(exc)) from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except F64CodecError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise F64CodecError("invalid JSON document: {}".format(exc)) from exc
    if not isinstance(value, Mapping):
        _fail("top-level JSON value must be an object")
    return raw, value


@dataclass(frozen=True)
class F64Array:
    """Validated bit-exact array decoded from a canonical wire document."""

    shape: Tuple[int, ...]
    bits: Tuple[str, ...]

    def __post_init__(self) -> None:
        """Keep direct construction inside the same invariants as decoding."""

        shape = _normalize_shape(self.shape)
        count = checked_element_count(shape)
        bits = _normalize_bits(self.bits, count)
        object.__setattr__(self, "shape", shape)
        object.__setattr__(self, "bits", bits)

    @property
    def values(self) -> Tuple[float, ...]:
        """Return the flattened C-row-major payload as native Python floats."""

        return tuple(bits_hex_to_f64(item) for item in self.bits)

    @property
    def canonical_bytes(self) -> bytes:
        """Return the unique canonical wire representation."""

        return _canonical_bytes_from_bits(self.shape, self.bits)

    @property
    def sha256(self) -> str:
        """Return lowercase SHA-256 of the canonical wire bytes."""

        return hashlib.sha256(self.canonical_bytes).hexdigest()


def decode_f64_array(document: Any) -> F64Array:
    """Strictly decode and validate one canonical finite-binary64 array."""

    raw, value = _parse_document(document)
    if set(value.keys()) != _EXACT_KEYS or len(value) != len(_EXACT_KEYS):
        _fail("array object must have exactly the keys bits and shape")
    # JSON arrays, rather than arbitrary Python tuples, are mandatory on input.
    if not isinstance(value["shape"], list):
        _fail("shape must be a JSON array")
    if not isinstance(value["bits"], list):
        _fail("bits must be a JSON array")
    shape = _normalize_shape(value["shape"])
    count = checked_element_count(shape)
    bits = _normalize_bits(value["bits"], count)
    decoded = F64Array(shape=shape, bits=bits)
    if raw != decoded.canonical_bytes:
        _fail("document is valid JSON but is not the canonical encoding")
    return decoded


def canonical_f64_array_sha256(document: Any) -> str:
    """Validate a canonical document and return its lowercase SHA-256 digest."""

    decoded = decode_f64_array(document)
    # Hash the reconstructed canonical form to keep the validation dependency
    # explicit even if the accepted bytes-like input was mutable.
    return decoded.sha256


def encode_f64_array_with_sha256(shape: Any, values: Any) -> Tuple[bytes, str]:
    """Return canonical bytes and their lowercase SHA-256 digest together."""

    document = encode_f64_array(shape, values)
    return document, hashlib.sha256(document).hexdigest()
