#!/usr/bin/env python3
"""Audit a KAIST VIO ZIP central directory without extracting its contents."""

import argparse
import json
import ntpath
import os
from pathlib import Path
import stat
import sys
import tempfile
import unicodedata
import zipfile


EXPECTED_RELATIVE_BAGS = (
    "circle/circle.bag",
    "circle/circle_fast.bag",
    "circle/circle_head.bag",
    "infinite/infinite.bag",
    "infinite/infinite_fast.bag",
    "infinite/infinite_head.bag",
    "rotation/rotation.bag",
    "rotation/rotation_fast.bag",
    "square/square.bag",
    "square/square_fast.bag",
    "square/square_head.bag",
)


class ArchiveAuditError(ValueError):
    """A deterministic archive-safety or layout failure."""

    def __init__(self, code, detail):
        super().__init__("{}: {}".format(code, detail))
        self.code = code
        self.detail = detail


def _fail(code, detail):
    raise ArchiveAuditError(code, detail)


def _validated_member_path(info):
    """Return a normalized member path and whether it denotes a directory."""
    name = info.filename
    if not name:
        _fail("empty_member_name", "ZIP member name is empty")
    if info.flag_bits & 0x1:
        _fail("encrypted_member", repr(name))
    if "\x00" in name:
        _fail("nul_in_member_name", repr(name))
    if "\\" in name:
        _fail("backslash_in_member_name", repr(name))
    if name.startswith("/"):
        _fail("absolute_member_path", repr(name))
    drive, _ = ntpath.splitdrive(name)
    if drive:
        _fail("drive_member_path", repr(name))

    is_directory = name.endswith("/")
    component_text = name[:-1] if is_directory else name
    if not component_text:
        _fail("empty_member_path", repr(name))
    components = component_text.split("/")
    if any(component == "" for component in components):
        _fail("empty_path_component", repr(name))
    if any(component in (".", "..") for component in components):
        _fail("dot_path_component", repr(name))

    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = unix_mode & 0o170000
    if file_type == stat.S_IFLNK:
        _fail("symlink_member", repr(name))
    if is_directory:
        if file_type not in (0, stat.S_IFDIR):
            _fail("directory_type_mismatch", repr(name))
    elif file_type not in (0, stat.S_IFREG):
        _fail("non_regular_member", repr(name))

    dos_directory = bool(info.external_attr & 0x10)
    if dos_directory and not is_directory:
        _fail("directory_marker_mismatch", repr(name))

    normalized = unicodedata.normalize("NFC", "/".join(components))
    return normalized, is_directory


def _resolve_bag_layout(bag_members):
    expected_parts = {
        relative: tuple(relative.split("/")) for relative in EXPECTED_RELATIVE_BAGS
    }
    mapping = {}
    prefixes = set()

    for member in sorted(bag_members):
        parts = tuple(member.split("/"))
        matches = [
            relative
            for relative, relative_parts in expected_parts.items()
            if len(parts) >= len(relative_parts)
            and parts[-len(relative_parts) :] == relative_parts
        ]
        if len(matches) != 1:
            _fail("unexpected_bag_member", repr(member))
        relative = matches[0]
        prefix = parts[: -len(expected_parts[relative])]
        if len(prefix) > 1:
            _fail("nested_archive_root_prefix", repr(member))
        if relative in mapping:
            _fail("duplicate_expected_bag", repr(relative))
        mapping[relative] = member
        prefixes.add(prefix)

    missing = sorted(set(EXPECTED_RELATIVE_BAGS) - set(mapping))
    if missing:
        _fail("missing_expected_bags", ",".join(missing))
    if len(mapping) != len(EXPECTED_RELATIVE_BAGS):
        _fail("unexpected_bag_count", str(len(mapping)))
    if len(prefixes) != 1:
        rendered = sorted("/".join(prefix) for prefix in prefixes)
        _fail("inconsistent_archive_root_prefix", repr(rendered))

    prefix = next(iter(prefixes))
    return (prefix[0] if prefix else ""), {
        relative: mapping[relative] for relative in sorted(mapping)
    }


def audit_archive(archive_path):
    """Return a deterministic audit report for one explicit ZIP archive path."""
    path = Path(archive_path)
    if not path.is_file():
        _fail("archive_not_regular_file", os.fspath(path))

    normalized_members = {}
    casefold_members = {}
    bag_members = []
    member_count = 0
    total_compressed_size = 0
    total_uncompressed_size = 0

    try:
        with zipfile.ZipFile(os.fspath(path), mode="r") as archive:
            for info in archive.infolist():
                normalized, is_directory = _validated_member_path(info)
                member_count += 1
                total_compressed_size += info.compress_size
                total_uncompressed_size += info.file_size

                if normalized in normalized_members:
                    _fail(
                        "normalized_member_collision",
                        repr((normalized_members[normalized], info.filename)),
                    )
                normalized_members[normalized] = info.filename

                folded = normalized.casefold()
                if folded in casefold_members:
                    _fail(
                        "casefold_member_collision",
                        repr((casefold_members[folded], info.filename)),
                    )
                casefold_members[folded] = info.filename

                if not is_directory and normalized.casefold().endswith(".bag"):
                    bag_members.append(normalized)
    except zipfile.BadZipFile as error:
        _fail("invalid_zip_archive", str(error))

    root_prefix, expected_mapping = _resolve_bag_layout(bag_members)
    return {
        "archive_member_count": member_count,
        "archive_path": os.fspath(path.resolve()),
        "expected_bag_members": expected_mapping,
        "root_prefix": root_prefix,
        "safety_flags": {
            "casefold_paths_unique": True,
            "central_directory_only": True,
            "expected_bag_layout_complete": True,
            "no_absolute_drive_or_ambiguous_paths": True,
            "normalized_paths_unique": True,
            "only_regular_files_and_directories": True,
        },
        "schema_version": 1,
        "total_compressed_size_bytes": total_compressed_size,
        "total_uncompressed_size_bytes": total_uncompressed_size,
    }


def write_json_atomic_no_overwrite(output_path, report):
    """Atomically publish sorted JSON while refusing every existing target."""
    output = Path(output_path)
    if not output.parent.is_dir():
        _fail("output_parent_not_directory", os.fspath(output.parent))
    if os.path.lexists(os.fspath(output)):
        _fail("output_exists", os.fspath(output))

    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(output.name), suffix=".tmp", dir=os.fspath(output.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, os.fspath(output))
        except FileExistsError:
            _fail("output_exists", os.fspath(output))
        os.unlink(temporary_name)
        temporary_name = None
        directory_descriptor = os.open(os.fspath(output.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _parse_arguments(argv):
    parser = argparse.ArgumentParser(
        description="Audit one KAIST VIO ZIP central directory without extraction."
    )
    parser.add_argument("archive", help="explicit path to the KAIST VIO ZIP")
    parser.add_argument(
        "--output", required=True, help="new JSON output path; existing paths are refused"
    )
    return parser.parse_args(argv)


def main(argv=None):
    arguments = _parse_arguments(argv)
    try:
        report = audit_archive(arguments.archive)
        write_json_atomic_no_overwrite(arguments.output, report)
    except (ArchiveAuditError, OSError) as error:
        if isinstance(error, ArchiveAuditError):
            code = error.code
            detail = error.detail
        else:
            code = "operating_system_error"
            detail = str(error)
        print(
            json.dumps(
                {"error": {"code": code, "detail": detail}, "ok": False},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
