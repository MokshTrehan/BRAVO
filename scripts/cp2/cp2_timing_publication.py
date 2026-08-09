#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Descriptor-bound, failure-atomic CP2-E artifact publication.

The timing artifact is first assembled and detached-verified under a hidden
directory in its final parent.  This module seals that exact directory inode,
durably synchronizes its closed regular-file inventory, and publishes it with
Linux ``renameat2(RENAME_NOREPLACE)``.  An exception or catchable signal after
the forward rename causes an exact-inode rollback and parent-directory fsync.
If the authoritative name or its durability cannot be reconciled, the result
is explicitly indeterminate and automatic deletion is forbidden.

No project registry, recorded input, ROS module, or host timing control is
read here.  The production entry point remains separately source-freeze
locked; protecting tests use only private synthetic directories in ``/tmp``.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import errno
import os
from pathlib import Path
import re
import stat
from typing import Callable, Optional, Tuple


RENAME_NOREPLACE = 1
AT_FDCWD = -100
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,255}$")


class TimingPublicationError(RuntimeError):
    """A timing artifact was not published."""


class TimingPublicationCollision(TimingPublicationError):
    """The final name already existed and neither inode was changed."""


class TimingPublicationIndeterminate(TimingPublicationError):
    """The exact authoritative name or its durability cannot be proved."""


def _fail(message: str) -> None:
    raise TimingPublicationError(message)


def _absolute_normal(value: object, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        _fail(label + " is not a path")
    text = os.fspath(value)
    if (
        not text
        or "\0" in text
        or not os.path.isabs(text)
        or os.path.normpath(text) != text
        or text == os.path.sep
    ):
        _fail(label + " is not normalized absolute non-root")
    path = Path(text)
    if SAFE_NAME.fullmatch(path.name) is None or path.name in (".", ".."):
        _fail(label + " basename is unsafe")
    return path


def _identity(value: os.stat_result) -> Tuple[int, int]:
    return value.st_dev, value.st_ino


def _open_parent(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        os.close(descriptor)
        _fail("publication parent is not an effective-user-owned real directory")
    if stat.S_IMODE(status.st_mode) & 0o022:
        os.close(descriptor)
        _fail("publication parent is group/world writable")
    return descriptor


def _revalidate_parent_path(parent: Path, held_fd: int) -> None:
    fresh = _open_parent(parent)
    try:
        if _identity(os.fstat(fresh)) != _identity(os.fstat(held_fd)):
            _fail("publication parent path no longer names the held directory")
    finally:
        os.close(fresh)


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=parent_fd)


def _stat_at(parent_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _renameat2_noreplace(parent_fd: int, old_name: str, new_name: str) -> None:
    """Use the one non-overwriting Linux rename primitive; never emulate it."""

    libc = ctypes.CDLL(None, use_errno=True)
    operation = getattr(libc, "renameat2", None)
    if operation is None:
        _fail("renameat2(RENAME_NOREPLACE) is unavailable")
    operation.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    operation.restype = ctypes.c_int
    result = operation(
        parent_fd,
        old_name.encode("utf-8", "strict"),
        parent_fd,
        new_name.encode("utf-8", "strict"),
        RENAME_NOREPLACE,
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), new_name)


def _walk_and_seal(directory_fd: int) -> None:
    """Fsync and make one descriptor-held regular tree immutable by mode."""

    root_status = os.fstat(directory_fd)
    if not stat.S_ISDIR(root_status.st_mode) or root_status.st_uid != os.geteuid():
        _fail("artifact directory is not an effective-user-owned directory")
    names = os.listdir(directory_fd)
    if names != sorted(names):
        names = sorted(names)
    if len(names) > 4096:
        _fail("artifact directory entry count exceeds the publication bound")
    for name in names:
        if SAFE_NAME.fullmatch(name) is None or name in (".", ".."):
            _fail("artifact contains an unsafe path component")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != os.geteuid():
            _fail("artifact member is not owned by the effective user")
        if stat.S_ISDIR(before.st_mode):
            child_fd = _open_directory_at(directory_fd, name)
            try:
                if _identity(os.fstat(child_fd)) != _identity(before):
                    _fail("artifact directory member changed while opening")
                _walk_and_seal(child_fd)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1:
                _fail("artifact contains a multiply linked regular file")
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                opened = os.fstat(file_fd)
                if (
                    _identity(opened) != _identity(before)
                    or not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                ):
                    _fail("artifact regular member changed while opening")
                os.fsync(file_fd)
                os.fchmod(file_fd, 0o444)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
        else:
            _fail("artifact contains a symlink or nonregular member")
    os.fchmod(directory_fd, 0o555)
    os.fsync(directory_fd)


def _walk_and_validate_sealed(directory_fd: int) -> None:
    root_status = os.fstat(directory_fd)
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) != 0o555
    ):
        _fail("artifact directory is not sealed 0555")
    names = sorted(os.listdir(directory_fd))
    if len(names) > 4096:
        _fail("artifact directory entry count exceeds the publication bound")
    for name in names:
        if SAFE_NAME.fullmatch(name) is None or name in (".", ".."):
            _fail("sealed artifact contains an unsafe path component")
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != os.geteuid():
            _fail("sealed artifact member has the wrong owner")
        if stat.S_ISDIR(before.st_mode):
            child_fd = _open_directory_at(directory_fd, name)
            try:
                if _identity(os.fstat(child_fd)) != _identity(before):
                    _fail("sealed artifact directory changed while opening")
                _walk_and_validate_sealed(child_fd)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o444:
                _fail("sealed artifact regular member mode/link count differs")
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                if _identity(os.fstat(file_fd)) != _identity(before):
                    _fail("sealed artifact regular member changed while opening")
            finally:
                os.close(file_fd)
        else:
            _fail("sealed artifact contains a symlink or nonregular member")


def seal_artifact_directory(path: object) -> Tuple[int, int]:
    """Seal and durably sync an unpublished directory, returning its identity."""

    staging = _absolute_normal(path, "artifact staging path")
    parent_fd = _open_parent(staging.parent)
    root_fd = -1
    try:
        _revalidate_parent_path(staging.parent, parent_fd)
        root_fd = _open_directory_at(parent_fd, staging.name)
        held = os.fstat(root_fd)
        by_name = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(held) != _identity(by_name):
            _fail("artifact staging name differs from the held directory")
        _walk_and_seal(root_fd)
        _walk_and_validate_sealed(root_fd)
        rebound = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(rebound) != _identity(held):
            _fail("artifact staging name changed during sealing")
        os.fsync(parent_fd)
        return _identity(held)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def _location(
    parent_fd: int,
    partial_name: str,
    final_name: str,
    held_identity: Tuple[int, int],
) -> str:
    partial = _stat_at(parent_fd, partial_name)
    final = _stat_at(parent_fd, final_name)
    partial_exact = partial is not None and _identity(partial) == held_identity
    final_exact = final is not None and _identity(final) == held_identity
    if partial_exact and final is None:
        return "partial"
    if final_exact and partial is None:
        return "final"
    if partial_exact and final is not None and not final_exact:
        return "collision"
    if final_exact and partial is not None and not partial_exact:
        return "final_with_obstruction"
    return "irreconcilable"


@dataclass(frozen=True)
class PublishedTimingArtifact:
    path: Path
    identity: Tuple[int, int]


BoundaryHook = Callable[[str], None]
RenameOperation = Callable[[int, str, str], None]
FsyncOperation = Callable[[int], None]
PublishedValidator = Callable[[Path], None]


def create_staging_directory(path: object) -> Tuple[int, int]:
    """Create one private hidden staging root relative to a held parent."""

    staging = _absolute_normal(path, "artifact staging path")
    if not staging.name.startswith(".") or ".partial." not in staging.name:
        _fail("artifact staging basename is not a hidden partial name")
    parent_fd = _open_parent(staging.parent)
    root_fd = -1
    try:
        _revalidate_parent_path(staging.parent, parent_fd)
        if _stat_at(parent_fd, staging.name) is not None:
            _fail("artifact staging path already exists")
        os.mkdir(staging.name, 0o700, dir_fd=parent_fd)
        root_fd = _open_directory_at(parent_fd, staging.name)
        held = os.fstat(root_fd)
        named = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(held) != _identity(named)
            or held.st_uid != os.geteuid()
            or stat.S_IMODE(held.st_mode) != 0o700
        ):
            _fail("artifact staging creation identity or mode differs")
        os.fsync(parent_fd)
        return _identity(held)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def publish_artifact_noreplace(
    staging_path: object,
    destination_path: object,
    *,
    rename_operation: RenameOperation = _renameat2_noreplace,
    fsync_operation: FsyncOperation = os.fsync,
    boundary_hook: Optional[BoundaryHook] = None,
    published_validator: Optional[PublishedValidator] = None,
    expected_staging_identity: Optional[Tuple[int, int]] = None,
) -> PublishedTimingArtifact:
    """Publish one already sealed directory or restore its exact partial name.

    The hook is a protecting-test seam.  Production callers leave it ``None``.
    Any exception it raises is treated like a catchable asynchronous failure.
    """

    staging = _absolute_normal(staging_path, "artifact staging path")
    destination = _absolute_normal(destination_path, "artifact destination path")
    if staging.parent != destination.parent or staging == destination:
        _fail("publication paths are not distinct siblings")
    if not callable(rename_operation) or not callable(fsync_operation):
        _fail("publication operation seam is not callable")
    hook = boundary_hook if boundary_hook is not None else (lambda _name: None)
    if not callable(hook):
        _fail("publication boundary hook is not callable")
    if published_validator is not None and not callable(published_validator):
        _fail("published validator is not callable")
    if expected_staging_identity is not None and (
        type(expected_staging_identity) is not tuple
        or len(expected_staging_identity) != 2
        or any(type(value) is not int or value < 0 for value in expected_staging_identity)
    ):
        _fail("expected staging identity is invalid")

    parent_fd = _open_parent(staging.parent)
    held_fd = -1
    held_identity: Optional[Tuple[int, int]] = None
    forward_attempted = False
    try:
        _revalidate_parent_path(staging.parent, parent_fd)
        held_fd = _open_directory_at(parent_fd, staging.name)
        held_status = os.fstat(held_fd)
        held_identity = _identity(held_status)
        if (
            expected_staging_identity is not None
            and held_identity != expected_staging_identity
        ):
            _fail("staging inode differs from the caller-held assembly identity")
        _walk_and_validate_sealed(held_fd)
        named = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(named) != held_identity:
            _fail("staging name differs from the held sealed inode")
        if _stat_at(parent_fd, destination.name) is not None:
            raise TimingPublicationCollision("artifact destination already exists")
        hook("before_parent_fsync")
        fsync_operation(parent_fd)
        hook("after_parent_fsync")
        _revalidate_parent_path(staging.parent, parent_fd)
        hook("before_forward_rename")
        forward_attempted = True
        rename_operation(parent_fd, staging.name, destination.name)
        hook("after_forward_rename")
        fsync_operation(parent_fd)
        hook("after_forward_fsync")
        try:
            _revalidate_parent_path(staging.parent, parent_fd)
        except TimingPublicationError as exc:
            raise TimingPublicationIndeterminate(
                "publication parent absolute path changed after rename"
            ) from exc
        if _location(parent_fd, staging.name, destination.name, held_identity) != "final":
            raise TimingPublicationIndeterminate(
                "forward publication left an irreconcilable held-inode binding"
            )
        final_fd = _open_directory_at(parent_fd, destination.name)
        try:
            if _identity(os.fstat(final_fd)) != held_identity:
                raise TimingPublicationIndeterminate(
                    "published name differs from the held artifact inode"
                )
            _walk_and_validate_sealed(final_fd)
            _walk_and_validate_sealed(held_fd)
        finally:
            os.close(final_fd)
        hook("after_final_identity_validation")
        if published_validator is not None:
            published_validator(destination)
        try:
            _revalidate_parent_path(staging.parent, parent_fd)
        except TimingPublicationError as exc:
            raise TimingPublicationIndeterminate(
                "publication parent absolute path changed during verification"
            ) from exc
        if _location(
            parent_fd, staging.name, destination.name, held_identity
        ) != "final":
            raise TimingPublicationIndeterminate(
                "published name changed during detached verification"
            )
        rebound_fd = _open_directory_at(parent_fd, destination.name)
        try:
            if _identity(os.fstat(rebound_fd)) != held_identity:
                raise TimingPublicationIndeterminate(
                    "published inode changed during detached verification"
                )
            _walk_and_validate_sealed(rebound_fd)
        finally:
            os.close(rebound_fd)
        hook("after_published_validation")
        try:
            _revalidate_parent_path(staging.parent, parent_fd)
        except TimingPublicationError as exc:
            raise TimingPublicationIndeterminate(
                "publication parent absolute path changed at the final return boundary"
            ) from exc
        if _location(
            parent_fd, staging.name, destination.name, held_identity
        ) != "final":
            raise TimingPublicationIndeterminate(
                "published name changed at the final return boundary"
            )
        final_fd = _open_directory_at(parent_fd, destination.name)
        try:
            if _identity(os.fstat(final_fd)) != held_identity:
                raise TimingPublicationIndeterminate(
                    "published inode changed at the final return boundary"
                )
            _walk_and_validate_sealed(final_fd)
            _walk_and_validate_sealed(held_fd)
        finally:
            os.close(final_fd)
        # The protecting hook models the last mutation window after the first
        # detached check.  Re-run the caller's manifest/hash validator here;
        # root-inode equality and 0444/0555 modes alone do not bind file bytes.
        if published_validator is not None:
            published_validator(destination)
        try:
            _revalidate_parent_path(staging.parent, parent_fd)
        except TimingPublicationError as exc:
            raise TimingPublicationIndeterminate(
                "publication parent path changed during final detached verification"
            ) from exc
        if _location(
            parent_fd, staging.name, destination.name, held_identity
        ) != "final":
            raise TimingPublicationIndeterminate(
                "published name changed during final detached verification"
            )
        terminal_fd = _open_directory_at(parent_fd, destination.name)
        try:
            if _identity(os.fstat(terminal_fd)) != held_identity:
                raise TimingPublicationIndeterminate(
                    "published inode changed during final detached verification"
                )
            _walk_and_validate_sealed(terminal_fd)
        finally:
            os.close(terminal_fd)
        return PublishedTimingArtifact(destination, held_identity)
    except BaseException as original:
        if held_identity is None or not forward_attempted:
            raise
        location = _location(parent_fd, staging.name, destination.name, held_identity)
        if location == "collision":
            if isinstance(original, OSError) and original.errno == errno.EEXIST:
                raise TimingPublicationCollision(
                    "artifact destination already exists"
                ) from original
            raise
        if location == "partial":
            raise
        if location != "final":
            raise TimingPublicationIndeterminate(
                "publication failure left an irreconcilable exact held inode"
            ) from original
        try:
            hook("before_rollback_rename")
            rename_operation(parent_fd, destination.name, staging.name)
            hook("after_rollback_rename")
            fsync_operation(parent_fd)
            hook("after_rollback_fsync")
        except BaseException as rollback_error:
            location_after = _location(
                parent_fd, staging.name, destination.name, held_identity
            )
            raise TimingPublicationIndeterminate(
                "publication rollback failed; exact durable state is indeterminate "
                "(observed {})".format(location_after)
            ) from rollback_error
        if _location(parent_fd, staging.name, destination.name, held_identity) != "partial":
            raise TimingPublicationIndeterminate(
                "publication rollback did not restore the exact partial inode"
            ) from original
        raise
    finally:
        if held_fd >= 0:
            os.close(held_fd)
        os.close(parent_fd)


def _remove_tree(directory_fd: int) -> None:
    status = os.fstat(directory_fd)
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.geteuid():
        _fail("cleanup root identity is unsafe")
    os.fchmod(directory_fd, 0o700)
    for name in sorted(os.listdir(directory_fd)):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if before.st_uid != os.geteuid():
            _fail("cleanup member ownership is unsafe")
        if stat.S_ISDIR(before.st_mode):
            child_fd = _open_directory_at(directory_fd, name)
            try:
                if _identity(os.fstat(child_fd)) != _identity(before):
                    _fail("cleanup directory member changed while opening")
                _remove_tree(child_fd)
            finally:
                os.close(child_fd)
            rebound = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(rebound) != _identity(before):
                _fail("cleanup directory member was substituted")
            os.rmdir(name, dir_fd=directory_fd)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            file_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                if _identity(os.fstat(file_fd)) != _identity(before):
                    _fail("cleanup regular member changed while opening")
                os.unlink(name, dir_fd=directory_fd)
                if os.fstat(file_fd).st_nlink != 0:
                    _fail("cleanup unlink did not detach the held regular inode")
            finally:
                os.close(file_fd)
        else:
            _fail("cleanup encountered an unsafe artifact member")
    os.fsync(directory_fd)


def cleanup_unpublished_artifact(
    staging_path: object, expected_identity: Tuple[int, int]
) -> None:
    """Remove only the exact caller-held unpublished inode, then fsync parent."""

    staging = _absolute_normal(staging_path, "artifact cleanup path")
    if (
        type(expected_identity) is not tuple
        or len(expected_identity) != 2
        or any(type(value) is not int or value < 0 for value in expected_identity)
    ):
        _fail("artifact cleanup identity is invalid")
    parent_fd = _open_parent(staging.parent)
    root_fd = -1
    try:
        _revalidate_parent_path(staging.parent, parent_fd)
        named = _stat_at(parent_fd, staging.name)
        if named is None or _identity(named) != expected_identity:
            _fail("refusing to clean a missing or substituted artifact staging name")
        root_fd = _open_directory_at(parent_fd, staging.name)
        if _identity(os.fstat(root_fd)) != expected_identity:
            _fail("cleanup path differs from the held artifact inode")
        _remove_tree(root_fd)
        rebound = os.stat(staging.name, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(rebound) != expected_identity:
            _fail("cleanup root was substituted before removal")
        os.rmdir(staging.name, dir_fd=parent_fd)
        if _stat_at(parent_fd, staging.name) is not None:
            _fail("cleanup staging name survived removal")
        os.fsync(parent_fd)
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


__all__ = [
    "PublishedTimingArtifact", "TimingPublicationCollision",
    "TimingPublicationError", "TimingPublicationIndeterminate",
    "cleanup_unpublished_artifact", "create_staging_directory",
    "publish_artifact_noreplace",
    "seal_artifact_directory",
]
