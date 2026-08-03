#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict, data-free decoding of CP2-D ``evo_ape`` result archives.

The parser intentionally implements only the small ZIP surface needed by the
reviewed CP2-D result transport.  It never extracts a member and decompresses
only the root ``stats.json`` member.  Every byte outside member payloads is
accounted for by an exact local-header/data, central-directory, or EOCD range.

This module does not read paths, discover an evaluator, or access recorded
inputs.  Callers are responsible for reading and independently hashing the
bounded archive bytes before passing them here.
"""

from __future__ import annotations

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

STAT_KEYS = ("max", "mean", "median", "min", "rmse", "sse", "std")
ARCHIVE_DIRECT_ABSOLUTE_TOLERANCE = 1.0e-12
ARCHIVE_DIRECT_RELATIVE_TOLERANCE = 1.0e-10


class EvoResultError(ValueError):
    """Raised for every malformed or inconsistent evaluator result."""


def _reject(message: str) -> None:
    raise EvoResultError(message)


def _checked_binary64(value: Any, label: str) -> float:
    if isinstance(value, bool) or type(value) not in (int, float):
        _reject(label + " must be a finite number")
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvoResultError(label + " must be a finite number") from exc
    if not math.isfinite(converted) or converted < 0.0:
        _reject(label + " must be finite and nonnegative")
    if converted == 0.0 and math.copysign(1.0, converted) < 0.0:
        _reject(label + " must not be negative zero")
    return converted


@dataclass(frozen=True)
class EvoStatistics:
    """The exact seven numeric values serialized by evo 1.31.1."""

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
        if not (self.min <= self.mean <= self.max):
            _reject("stats mean is outside min/max")
        if not (self.min <= self.rmse <= self.max):
            _reject("stats RMSE is outside min/max")
        # For a finite population of nonnegative translation errors,
        # sqrt(E[x^2]) >= E[x].  The retained values are already binary64, so
        # this is an exact ordered comparison: allowing even a one-ULP
        # inversion would admit a statistic tuple that no such population can
        # produce.  Equality is valid for a constant-error population.
        if self.rmse < self.mean:
            _reject("stats RMSE is below the arithmetic mean")
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
        raise EvoResultError(label + " is not representable as binary64") from exc
    if not decimal_value.is_finite() or not math.isfinite(converted):
        _reject(label + " must be finite")
    if decimal_value < 0 or (decimal_value.is_zero() and decimal_value.is_signed()):
        _reject(label + " must be nonnegative and not negative zero")
    if not decimal_value.is_zero() and converted == 0.0:
        _reject(label + " underflows binary64")
    return converted


def _parse_stats_json(document: bytes) -> EvoStatistics:
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvoResultError("stats.json is not strict UTF-8") from exc
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_strict_pairs,
            parse_int=_JsonNumber,
            parse_float=_JsonNumber,
            parse_constant=_reject_json_constant,
        )
    except EvoResultError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise EvoResultError("stats.json is invalid JSON") from exc
    if not isinstance(parsed, Mapping) or set(parsed) != set(STAT_KEYS):
        _reject("stats.json must contain exactly the seven frozen statistic keys")
    values = {key: _json_binary64(parsed[key], "stats." + key) for key in STAT_KEYS}
    return EvoStatistics(**values)


def _decode_name(encoded: bytes) -> str:
    if not encoded or len(encoded) > MAX_NAME_BYTES:
        _reject("ZIP member name length is outside the frozen bound")
    try:
        name = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise EvoResultError("ZIP member name is not strict UTF-8") from exc
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


def _decompress_stats(data: bytes, member: _LocatedMember) -> bytes:
    metadata = member.central
    if metadata.uncompressed_size > MAX_STATS_BYTES:
        _reject("stats.json exceeds the frozen uncompressed bound")
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
            raise EvoResultError("stats.json raw DEFLATE stream is invalid") from exc
        if len(result) > metadata.uncompressed_size or decoder.unconsumed_tail:
            _reject("stats.json DEFLATE output exceeds its declared size")
        try:
            result += decoder.flush(metadata.uncompressed_size + 1 - len(result))
        except zlib.error as exc:
            raise EvoResultError("stats.json raw DEFLATE stream is invalid") from exc
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            _reject("stats.json raw DEFLATE stream has missing or trailing data")
    if len(result) != metadata.uncompressed_size:
        _reject("stats.json uncompressed size differs")
    if (zlib.crc32(result) & 0xFFFFFFFF) != metadata.crc32:
        _reject("stats.json CRC-32 differs")
    return result


def parse_evo_result_zip(archive: bytes) -> EvoStatistics:
    """Validate *archive* byte-for-byte structurally and return ``stats.json``.

    Only classic single-disk ZIP with STORE or raw DEFLATE members is accepted.
    Non-``stats.json`` payloads are bounded and located but never decompressed.
    """

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
    located = _locate_member_data(archive, members, central_offset)
    stats_member = next(member for member in located if member.central.name == "stats.json")
    return _parse_stats_json(_decompress_stats(archive, stats_member))


def expected_console_rmse(archive_rmse: Any) -> str:
    """Return evo 1.31.1's exact presentation-only RMSE token."""

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
