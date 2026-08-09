#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict decoding of the CP2-D equivalent-evaluator result archive.

The parser intentionally implements only the small ZIP surface needed by the
reviewed CP2-D result transport.  It never extracts a member and decompresses
only the root ``stats.json`` member.  Every byte outside member payloads is
accounted for by an exact local-header/data, central-directory, or EOCD range.

This module does not read paths, discover an evaluator, or access recorded
inputs.  Callers are responsible for reading and independently hashing the
bounded archive bytes before passing them here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import stat
import struct
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple
import unicodedata
import zlib


EOCD_SIGNATURE = 0x06054B50
CENTRAL_SIGNATURE = 0x02014B50
LOCAL_SIGNATURE = 0x04034B50
ZIP64_EXTRA_ID = 0x0001
UTF8_FLAG = 1 << 11
ALLOWED_FLAGS = UTF8_FLAG
STORE = 0
DEFLATE = 8

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_COUNT = 64
MAX_NAME_BYTES = 1024
MAX_EXTRA_BYTES = 4096
MAX_MEMBER_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_TOTAL_COMPRESSED_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_STATS_BYTES = 64 * 1024
MAX_INFO_BYTES = 64 * 1024
MAX_ERROR_ARRAY_BYTES = 64 * 1024 * 1024

STAT_KEYS = ("max", "mean", "median", "min", "rmse", "sse", "std")
EXPECTED_INFO_JSON = (
    b'{"title":"APE w.r.t. translation part (m)",'
    b'"ref_name":"ground-truth common aligned population",'
    b'"est_name":"estimate common aligned population",'
    b'"label":"ape_translation_rmse"}'
)
ARCHIVE_DIRECT_ABSOLUTE_TOLERANCE = 1.0e-12
ARCHIVE_DIRECT_RELATIVE_TOLERANCE = 1.0e-10


class EvaluatorResultError(ValueError):
    """Raised for every malformed or inconsistent evaluator result."""


def _reject(message: str) -> None:
    raise EvaluatorResultError(message)


def _checked_binary64(value: Any, label: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        _reject(label + " must be a finite number")
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvaluatorResultError(label + " must be a finite number") from exc
    if not math.isfinite(converted) or converted < 0.0:
        _reject(label + " must be finite and nonnegative")
    if converted == 0.0 and math.copysign(1.0, converted) < 0.0:
        _reject(label + " must not be negative zero")
    return converted


@dataclass(frozen=True)
class EvaluatorStatistics:
    """The exact seven values serialized by the equivalent evaluator."""

    max: float
    mean: float
    median: float
    min: float
    rmse: float
    sse: float
    std: float

    def __post_init__(self) -> None:
        for key in STAT_KEYS:
            object.__setattr__(self, key, _checked_binary64(getattr(self, key), "stats." + key))
        if not (self.min <= self.median <= self.max):
            _reject("stats min/median/max ordering is impossible")
        # Do not apply real-arithmetic mean/RMSE inequalities to the retained
        # binary64 values.  The evaluator deliberately uses ordered binary64
        # sums, division, and sqrt; valid populations can round mean above max,
        # RMSE above max, or RMSE below mean by one ULP.  The population-bearing
        # parser below reconstructs all seven values in the exact frozen order
        # and compares every bit, which is the normative impossibility check.
        if self.max == 0.0 and any(
            getattr(self, key) != 0.0 for key in ("mean", "median", "min", "rmse", "sse", "std")
        ):
            _reject("zero maximum is inconsistent with nonzero statistics")

    def as_mapping(self) -> Mapping[str, float]:
        return {key: getattr(self, key) for key in STAT_KEYS}


@dataclass(frozen=True)
class RmseAgreement:
    """Retained terms of the frozen archive/direct RMSE comparison."""

    archive_rmse: float
    direct_rmse: float
    absolute_difference: float
    permitted_difference: float

    def __post_init__(self) -> None:
        archive = _checked_binary64(self.archive_rmse, "archive RMSE")
        direct = _checked_binary64(self.direct_rmse, "direct RMSE")
        difference = _checked_binary64(self.absolute_difference, "RMSE absolute difference")
        permitted = _checked_binary64(self.permitted_difference, "RMSE permitted difference")
        expected_difference = abs(archive - direct)
        expected_permitted = ARCHIVE_DIRECT_ABSOLUTE_TOLERANCE + (
            ARCHIVE_DIRECT_RELATIVE_TOLERANCE * abs(direct)
        )
        if (
            struct.pack(">d", difference) != struct.pack(">d", expected_difference)
            or struct.pack(">d", permitted) != struct.pack(">d", expected_permitted)
            or difference > permitted
        ):
            _reject("retained RMSE-agreement terms are internally inconsistent")
        object.__setattr__(self, "archive_rmse", archive)
        object.__setattr__(self, "direct_rmse", direct)
        object.__setattr__(self, "absolute_difference", difference)
        object.__setattr__(self, "permitted_difference", permitted)


@dataclass(frozen=True)
class EvaluatorResultArchive:
    statistics: EvaluatorStatistics
    error_count: int
    error_bits: Tuple[str, ...]
    member_names: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.statistics, EvaluatorStatistics):
            _reject("evaluator archive statistics have the wrong type")
        if type(self.error_count) is not int or self.error_count <= 0:
            _reject("evaluator error population count must be positive")
        if type(self.error_bits) is not tuple or len(self.error_bits) != self.error_count:
            _reject("evaluator error bit population differs from its count")
        for item in self.error_bits:
            if type(item) is not str or len(item) != 16:
                _reject("evaluator error bit payload is malformed")
        if self.member_names != ("error_array.npy", "info.json", "stats.json"):
            _reject("evaluator archive member inventory differs")


@dataclass(frozen=True)
class _CentralMember:
    name: str
    name_bytes: bytes
    version_needed: int
    flags: int
    compression: int
    modification_time: int
    modification_date: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    extra: bytes
    local_offset: int


@dataclass(frozen=True)
class _LocatedMember:
    central: _CentralMember
    data_start: int
    data_end: int


class _JsonNumber:
    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        self.token = token


def _strict_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _reject("stats.json contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> None:
    _reject("stats.json contains a non-JSON numeric constant: " + token)


def _json_binary64(value: Any, label: str) -> float:
    if not isinstance(value, _JsonNumber):
        _reject(label + " must be a JSON number")
    try:
        decimal_value = Decimal(value.token)
        converted = float(value.token)
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise EvaluatorResultError(label + " is not representable as binary64") from exc
    if not decimal_value.is_finite() or not math.isfinite(converted):
        _reject(label + " must be finite")
    if decimal_value < 0 or (decimal_value.is_zero() and decimal_value.is_signed()):
        _reject(label + " must be nonnegative and not negative zero")
    if not decimal_value.is_zero() and converted == 0.0:
        _reject(label + " underflows binary64")
    return converted


def _parse_stats_json(document: bytes) -> EvaluatorStatistics:
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvaluatorResultError("stats.json is not strict UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_pairs,
            parse_int=_JsonNumber,
            parse_float=_JsonNumber,
            parse_constant=_reject_json_constant,
        )
    except EvaluatorResultError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvaluatorResultError("stats.json is invalid JSON") from exc
    if not isinstance(parsed, Mapping) or set(parsed) != set(STAT_KEYS):
        _reject("stats.json must contain exactly the seven frozen statistic keys")
    values = {key: _json_binary64(parsed[key], "stats." + key) for key in STAT_KEYS}
    return EvaluatorStatistics(**values)


def _decode_name(encoded: bytes) -> str:
    if not encoded or len(encoded) > MAX_NAME_BYTES:
        _reject("ZIP member name length is outside the frozen bound")
    try:
        name = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvaluatorResultError("ZIP member name is not strict UTF-8") from exc
    if unicodedata.normalize("NFC", name) != name:
        _reject("ZIP member name is not NFC-normalized")
    if (
        name.startswith("/")
        or name.endswith("/")
        or "\\" in name
        or "\x00" in name
        or ":" in name
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
    ):
        _reject("ZIP member name is unsafe")
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        _reject("ZIP member name is not a normalized relative path")
    return name


def _validate_extra(extra: bytes) -> None:
    # The future evaluator profile must be generated with no extras.  A closed
    # surface is preferable to interpreting an extensible metadata namespace
    # (including type/path override and ZIP64 variants) during detached review.
    if extra:
        _reject("ZIP extra fields are forbidden")


def _validate_regular_member(version_made_by: int, external_attributes: int, name: str) -> None:
    host_system = version_made_by >> 8
    creator_version = version_made_by & 0xFF
    dos_attributes = external_attributes & 0xFF
    if dos_attributes & 0x10 or name.endswith("/"):
        _reject("ZIP directory members are forbidden")
    if host_system != 3 or creator_version != 20:
        _reject("ZIP creator identity must be Unix ZIP 2.0")
    mode = (external_attributes >> 16) & 0xFFFF
    if stat.S_IFMT(mode) != stat.S_IFREG:
        _reject("ZIP member is not explicitly a regular Unix file")


def _read_central_members(data: bytes, offset: int, size: int, count: int) -> Tuple[_CentralMember, ...]:
    end = offset + size
    cursor = offset
    result: List[_CentralMember] = []
    names = set()
    local_offsets = set()
    for _ in range(count):
        if cursor + 46 > end:
            _reject("ZIP central-directory header is truncated")
        fields = struct.unpack_from("<I6H3I5H2I", data, cursor)
        if fields[0] != CENTRAL_SIGNATURE:
            _reject("ZIP central-directory signature differs")
        (
            _,
            version_made_by,
            version_needed,
            flags,
            compression,
            modification_time,
            modification_date,
            crc32_value,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            comment_length,
            disk_start,
            _internal_attributes,
            external_attributes,
            local_offset,
        ) = fields
        record_end = cursor + 46 + name_length + extra_length + comment_length
        if record_end > end:
            _reject("ZIP central-directory record is truncated")
        if comment_length != 0:
            _reject("ZIP member comments are forbidden")
        if disk_start != 0:
            _reject("multi-disk ZIP members are forbidden")
        if (
            (compression == STORE and version_needed not in (10, 20))
            or (compression == DEFLATE and version_needed != 20)
        ):
            _reject("ZIP extraction version is inconsistent with its compression method")
        if flags & ~ALLOWED_FLAGS:
            _reject("encrypted, descriptor, or unsupported ZIP flags are forbidden")
        if compression not in (STORE, DEFLATE):
            _reject("unsupported ZIP compression method")
        if compressed_size == 0xFFFFFFFF or uncompressed_size == 0xFFFFFFFF or local_offset == 0xFFFFFFFF:
            _reject("ZIP64 sentinel is forbidden")
        if compressed_size > MAX_MEMBER_COMPRESSED_BYTES:
            _reject("ZIP member compressed size exceeds the frozen bound")
        if uncompressed_size > MAX_MEMBER_UNCOMPRESSED_BYTES:
            _reject("ZIP member uncompressed size exceeds the frozen bound")
        if compression == STORE and compressed_size != uncompressed_size:
            _reject("stored ZIP member has unequal compressed and uncompressed sizes")
        name_bytes = data[cursor + 46 : cursor + 46 + name_length]
        extra = data[cursor + 46 + name_length : cursor + 46 + name_length + extra_length]
        name = _decode_name(name_bytes)
        if any(value >= 0x80 for value in name_bytes) and not (flags & UTF8_FLAG):
            _reject("non-ASCII ZIP member name lacks the UTF-8 flag")
        _validate_extra(extra)
        _validate_regular_member(version_made_by, external_attributes, name)
        if name in names:
            _reject("ZIP member name is duplicated")
        if local_offset in local_offsets:
            _reject("ZIP local-header offset is duplicated")
        names.add(name)
        local_offsets.add(local_offset)
        result.append(
            _CentralMember(
                name=name,
                name_bytes=name_bytes,
                version_needed=version_needed,
                flags=flags,
                compression=compression,
                modification_time=modification_time,
                modification_date=modification_date,
                crc32=crc32_value,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                extra=extra,
                local_offset=local_offset,
            )
        )
        cursor = record_end
    if cursor != end:
        _reject("ZIP central directory contains a gap or unparsed bytes")
    if "stats.json" not in names:
        _reject("ZIP archive has no root stats.json member")
    return tuple(result)


def _locate_member_data(data: bytes, members: Iterable[_CentralMember], central_offset: int) -> Tuple[_LocatedMember, ...]:
    cursor = 0
    located: List[_LocatedMember] = []
    total_compressed = 0
    total_uncompressed = 0
    for member in sorted(members, key=lambda value: value.local_offset):
        if member.local_offset != cursor:
            _reject("ZIP local records overlap or leave a preamble/gap")
        if cursor + 30 > central_offset:
            _reject("ZIP local header is truncated")
        fields = struct.unpack_from("<I5H3I2H", data, cursor)
        if fields[0] != LOCAL_SIGNATURE:
            _reject("ZIP local-header signature differs")
        (
            _,
            version_needed,
            flags,
            compression,
            modification_time,
            modification_date,
            crc32_value,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
        ) = fields
        header_end = cursor + 30 + name_length + extra_length
        data_end = header_end + compressed_size
        if header_end > central_offset or data_end > central_offset:
            _reject("ZIP local record extends into the central directory")
        name_bytes = data[cursor + 30 : cursor + 30 + name_length]
        extra = data[cursor + 30 + name_length : header_end]
        if (
            version_needed != member.version_needed
            or flags != member.flags
            or compression != member.compression
            or modification_time != member.modification_time
            or modification_date != member.modification_date
            or crc32_value != member.crc32
            or compressed_size != member.compressed_size
            or uncompressed_size != member.uncompressed_size
            or name_bytes != member.name_bytes
            or extra != member.extra
        ):
            _reject("ZIP local and central metadata differ")
        _validate_extra(extra)
        total_compressed += compressed_size
        total_uncompressed += uncompressed_size
        if total_compressed > MAX_TOTAL_COMPRESSED_BYTES:
            _reject("ZIP total compressed size exceeds the frozen bound")
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED_BYTES:
            _reject("ZIP total uncompressed size exceeds the frozen bound")
        located.append(_LocatedMember(member, header_end, data_end))
        cursor = data_end
    if cursor != central_offset:
        _reject("ZIP local records do not exactly meet the central directory")
    return tuple(located)


def _decompress_member(
    data: bytes, member: _LocatedMember, maximum_size: int, label: str
) -> bytes:
    metadata = member.central
    if metadata.uncompressed_size > maximum_size:
        _reject(label + " exceeds the frozen uncompressed bound")
    compressed = data[member.data_start : member.data_end]
    if metadata.compression == STORE:
        if metadata.compressed_size != metadata.uncompressed_size:
            _reject("stored stats.json has unequal compressed and uncompressed sizes")
        result = compressed
    else:
        decoder = zlib.decompressobj(-zlib.MAX_WBITS)
        try:
            result = decoder.decompress(compressed, metadata.uncompressed_size + 1)
        except zlib.error as exc:
            raise EvaluatorResultError(label + " raw DEFLATE stream is invalid") from exc
        if len(result) > metadata.uncompressed_size or decoder.unconsumed_tail:
            _reject(label + " DEFLATE output exceeds its declared size")
        try:
            result += decoder.flush(metadata.uncompressed_size + 1 - len(result))
        except zlib.error as exc:
            raise EvaluatorResultError(label + " raw DEFLATE stream is invalid") from exc
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            _reject(label + " raw DEFLATE stream has missing or trailing data")
    if len(result) != metadata.uncompressed_size:
        _reject(label + " uncompressed size differs")
    if (zlib.crc32(result) & 0xFFFFFFFF) != metadata.crc32:
        _reject(label + " CRC-32 differs")
    return result


def _decompress_stats(data: bytes, member: _LocatedMember) -> bytes:
    return _decompress_member(data, member, MAX_STATS_BYTES, "stats.json")


def _parse_info_json(document: bytes) -> None:
    if document != EXPECTED_INFO_JSON:
        _reject("info.json differs from the exact equivalent-evaluator identity")
    try:
        value = json.loads(
            document.decode("utf-8", "strict"),
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_json_constant,
        )
    except EvaluatorResultError:
        raise
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError) as exc:
        raise EvaluatorResultError("info.json is not strict JSON") from exc
    if value != {
        "title": "APE w.r.t. translation part (m)",
        "ref_name": "ground-truth common aligned population",
        "est_name": "estimate common aligned population",
        "label": "ape_translation_rmse",
    }:
        _reject("info.json equivalent-evaluator identity differs")


def _parse_error_array(document: bytes) -> Tuple[str, ...]:
    if len(document) < 10 or document[:6] != b"\x93NUMPY":
        _reject("error_array.npy lacks the NumPy magic")
    major = document[6]
    minor = document[7]
    if (major, minor) == (1, 0):
        if len(document) < 10:
            _reject("error_array.npy v1 header is truncated")
        header_length = struct.unpack_from("<H", document, 8)[0]
        header_start = 10
    elif (major, minor) in ((2, 0), (3, 0)):
        if len(document) < 12:
            _reject("error_array.npy header is truncated")
        header_length = struct.unpack_from("<I", document, 8)[0]
        header_start = 12
    else:
        _reject("error_array.npy format version is unsupported")
    header_end = header_start + header_length
    if header_length == 0 or header_end > len(document):
        _reject("error_array.npy header length exceeds its member")
    header_bytes = document[header_start:header_end]
    if not header_bytes.endswith(b"\n"):
        _reject("error_array.npy header lacks its terminal LF")
    encoding = "latin1" if major < 3 else "utf-8"
    try:
        header = ast.literal_eval(header_bytes.decode(encoding, "strict").strip())
    except (UnicodeDecodeError, ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        raise EvaluatorResultError("error_array.npy header is not a literal mapping") from exc
    if type(header) is not dict or set(header) != {"descr", "fortran_order", "shape"}:
        _reject("error_array.npy header keys differ")
    if header["descr"] != "<f8" or header["fortran_order"] is not False:
        _reject("error_array.npy must be little-endian C-order binary64")
    shape = header["shape"]
    if type(shape) is not tuple or len(shape) != 1 or type(shape[0]) is not int or shape[0] <= 0:
        _reject("error_array.npy must have one positive dimension")
    count = shape[0]
    if count > MAX_ERROR_ARRAY_BYTES // 8 or len(document) - header_end != count * 8:
        _reject("error_array.npy payload size differs from its shape")
    bits = []
    for index in range(count):
        value = struct.unpack_from("<d", document, header_end + 8 * index)[0]
        if (
            not math.isfinite(value)
            or value < 0.0
            or (value == 0.0 and math.copysign(1.0, value) < 0.0)
        ):
            _reject("error_array.npy contains a nonfinite or negative error")
        bits.append(struct.pack(">d", value).hex())
    return tuple(bits)


def _ordered_binary64_sum(values: Sequence[float], label: str) -> float:
    total = 0.0
    for value in values:
        total = total + value
        if not math.isfinite(total):
            _reject(label + " ordered sum became nonfinite")
    return total


def _statistics_from_error_bits(error_bits: Sequence[str]) -> EvaluatorStatistics:
    """Independently replay the equivalent evaluator's exact seven statistics."""

    if not error_bits or len(error_bits) > MAX_ERROR_ARRAY_BYTES // 8:
        _reject("evaluator error population count is outside its bound")
    values = tuple(struct.unpack(">d", bytes.fromhex(bits))[0] for bits in error_bits)
    count = len(values)
    if count > (1 << 53):
        _reject("evaluator error count exceeds exact binary64")
    ordered = tuple(sorted(values))
    if count & 1:
        median = ordered[count // 2]
    else:
        median = (ordered[count // 2 - 1] + ordered[count // 2]) / 2.0
    mean = _ordered_binary64_sum(values, "error mean") / float(count)
    squared = tuple(value * value for value in values)
    if any(not math.isfinite(value) for value in squared):
        _reject("evaluator squared error became nonfinite")
    sse = _ordered_binary64_sum(squared, "error SSE")
    rmse = math.sqrt(sse / float(count))
    deviations = tuple((value - mean) * (value - mean) for value in values)
    if any(not math.isfinite(value) for value in deviations):
        _reject("evaluator squared deviation became nonfinite")
    variance = _ordered_binary64_sum(deviations, "error variance") / float(count)
    return EvaluatorStatistics(
        max=ordered[-1],
        mean=mean,
        median=median,
        min=ordered[0],
        rmse=rmse,
        sse=sse,
        std=math.sqrt(variance),
    )


def _require_population_statistics(
    retained: EvaluatorStatistics, error_bits: Sequence[str]
) -> None:
    reconstructed = _statistics_from_error_bits(error_bits)
    for key in STAT_KEYS:
        if struct.pack(">d", getattr(retained, key)) != struct.pack(
            ">d", getattr(reconstructed, key)
        ):
            _reject("stats.{} differs from the retained error population".format(key))


def _located_archive(archive: bytes) -> Tuple[_LocatedMember, ...]:
    if type(archive) is not bytes:
        _reject("evaluator result archive must be immutable bytes")
    if len(archive) < 22 or len(archive) > MAX_ARCHIVE_BYTES:
        _reject("evaluator result archive size is outside the frozen bound")
    eocd_offset = len(archive) - 22
    fields = struct.unpack_from("<I4H2IH", archive, eocd_offset)
    if fields[0] != EOCD_SIGNATURE:
        _reject("ZIP EOCD is absent from the exact end position")
    (
        _,
        disk_number,
        central_disk,
        disk_entry_count,
        total_entry_count,
        central_size,
        central_offset,
        comment_length,
    ) = fields
    if comment_length != 0:
        _reject("ZIP EOCD comments or trailing bytes are forbidden")
    if disk_number != 0 or central_disk != 0 or disk_entry_count != total_entry_count:
        _reject("multi-disk or inconsistent ZIP EOCD is forbidden")
    if total_entry_count in (0, 0xFFFF) or total_entry_count > MAX_MEMBER_COUNT:
        _reject("ZIP member count is outside the frozen bound or uses ZIP64")
    if central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        _reject("ZIP64 EOCD sentinel is forbidden")
    if central_offset + central_size != eocd_offset:
        _reject("ZIP central directory leaves a gap, overlaps, or has trailing data")
    members = _read_central_members(archive, central_offset, central_size, total_entry_count)
    return _locate_member_data(archive, members, central_offset)


def parse_evaluator_result_zip(archive: bytes) -> EvaluatorStatistics:
    """Validate ZIP structure and return the exact ``stats.json`` values."""

    located = _located_archive(archive)
    stats_member = next(member for member in located if member.central.name == "stats.json")
    return _parse_stats_json(_decompress_stats(archive, stats_member))


def parse_evaluator_result_archive(archive: bytes) -> EvaluatorResultArchive:
    """Require the exact equivalent-API population-bearing surface."""

    located = _located_archive(archive)
    by_name = {member.central.name: member for member in located}
    expected = {"info.json", "stats.json", "error_array.npy"}
    if set(by_name) != expected:
        _reject("evaluator archive must contain exactly info, stats, and error array")
    info = _decompress_member(archive, by_name["info.json"], MAX_INFO_BYTES, "info.json")
    _parse_info_json(info)
    stats = _parse_stats_json(_decompress_stats(archive, by_name["stats.json"]))
    error_document = _decompress_member(
        archive,
        by_name["error_array.npy"],
        MAX_ERROR_ARRAY_BYTES,
        "error_array.npy",
    )
    error_bits = _parse_error_array(error_document)
    _require_population_statistics(stats, error_bits)
    return EvaluatorResultArchive(
        stats,
        len(error_bits),
        error_bits,
        tuple(sorted(expected)),
    )


def expected_console_rmse(archive_rmse: Any) -> str:
    """Return the equivalent evaluator's presentation-only RMSE token."""

    value = _checked_binary64(archive_rmse, "archive RMSE")
    return "{:.6f}".format(value)


def require_console_rmse_match(console_token: Any, archive_rmse: Any) -> str:
    """Require the retained console RMSE token to match ``{:.6f}`` exactly."""

    if type(console_token) is not str:
        _reject("console RMSE token must be text")
    expected = expected_console_rmse(archive_rmse)
    if console_token != expected:
        _reject("console RMSE token differs from the archive's six-decimal rendering")
    return expected


def require_archive_direct_rmse_agreement(archive_rmse: Any, direct_rmse: Any) -> RmseAgreement:
    """Apply the frozen tight comparison, using direct RMSE as ``reference``."""

    archive_value = _checked_binary64(archive_rmse, "archive RMSE")
    direct_value = _checked_binary64(direct_rmse, "direct RMSE")
    difference = abs(archive_value - direct_value)
    permitted = ARCHIVE_DIRECT_ABSOLUTE_TOLERANCE + (
        ARCHIVE_DIRECT_RELATIVE_TOLERANCE * abs(direct_value)
    )
    if not math.isfinite(difference) or not math.isfinite(permitted) or difference > permitted:
        _reject("archive RMSE and direct RMSE exceed the frozen tolerance")
    return RmseAgreement(archive_value, direct_value, difference, permitted)


# Legacy construction-time compatibility only.  Formal CP2-D provenance,
# artifact names, and new callers use the evaluator-neutral identities above;
# these aliases do not assert evo software or archive provenance.
EvoResultError = EvaluatorResultError
EvoStatistics = EvaluatorStatistics
EvoResultArchive = EvaluatorResultArchive
parse_evo_result_zip = parse_evaluator_result_zip
parse_evo_result_archive = parse_evaluator_result_archive


__all__ = [
    "ARCHIVE_DIRECT_ABSOLUTE_TOLERANCE",
    "ARCHIVE_DIRECT_RELATIVE_TOLERANCE",
    "EXPECTED_INFO_JSON",
    "EvaluatorResultArchive",
    "EvaluatorResultError",
    "EvaluatorStatistics",
    "RmseAgreement",
    "expected_console_rmse",
    "parse_evaluator_result_archive",
    "parse_evaluator_result_zip",
    "require_archive_direct_rmse_agreement",
    "require_console_rmse_match",
]
