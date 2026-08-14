#!/usr/bin/env python3
"""Create a canonical, independently resolved TurnSafe source snapshot."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


SCHEMA = "turnsafe.source_snapshot.v1"
PROTECTED_PREFIX = "scripts/cp2/"
COMPILED_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
}


class SnapshotError(RuntimeError):
    """A fail-closed source-snapshot error."""


def _git(repository: Path, arguments: Sequence[str]) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository)] + list(arguments),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
        },
    )
    if completed.returncode != 0:
        raise SnapshotError(
            "git {} failed: {}".format(
                " ".join(arguments),
                completed.stderr.decode("utf-8", errors="replace").strip(),
            )
        )
    return completed.stdout


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _is_compiled_input(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.suffix.lower() in COMPILED_SUFFIXES
        or candidate.name == "CMakeLists.txt"
        or candidate.suffix.lower() == ".cmake"
    )


def _parse_porcelain(raw: bytes) -> List[Tuple[str, str]]:
    fields = raw.split(b"\0")
    entries: List[Tuple[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        record = fields[index]
        index += 1
        if len(record) < 4 or record[2:3] != b" ":
            raise SnapshotError("unexpected git porcelain record")
        code = record[:2].decode("ascii", errors="strict")
        path = record[3:].decode("utf-8", errors="surrogateescape")
        entries.append((code, path))
        if "R" in code or "C" in code:
            if index >= len(fields) or not fields[index]:
                raise SnapshotError("truncated rename/copy porcelain record")
            original = fields[index].decode(
                "utf-8", errors="surrogateescape"
            )
            index += 1
            entries.append(("OR", original))
    return entries


def _file_records(repository: Path, paths: Iterable[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for relative in sorted(set(paths)):
        if relative == "scripts/cp2" or relative.startswith(PROTECTED_PREFIX):
            raise SnapshotError("protected scripts/cp2 path entered snapshot")
        absolute = repository / relative
        if not absolute.is_file() or absolute.is_symlink():
            raise SnapshotError("snapshot input is not a regular file: {}".format(relative))
        records.append(
            {
                "path": relative,
                "size_bytes": absolute.stat().st_size,
                "sha256": _sha256_file(absolute),
            }
        )
    return records


def create_snapshot(repository: Path) -> Dict[str, Any]:
    repository = repository.resolve(strict=True)
    if not (repository / ".git").exists():
        raise SnapshotError("repository is not a Git worktree")

    head_sha = _git(repository, ["rev-parse", "HEAD"]).decode("ascii").strip()
    head_tree = _git(repository, ["rev-parse", "HEAD^{tree}"]).decode(
        "ascii"
    ).strip()
    status_raw = _git(
        repository,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            ".",
            ":(exclude)scripts/cp2/**",
        ],
    )
    status_entries = _parse_porcelain(status_raw)

    tracked_diff = _git(
        repository,
        [
            "diff",
            "HEAD",
            "--binary",
            "--no-ext-diff",
            "--",
            ".",
            ":(exclude)scripts/cp2/**",
        ],
    )
    tracked_paths = _git(
        repository, ["ls-files", "-z", "--", "ov_core", "ov_msckf"]
    ).split(b"\0")
    compiled_paths = [
        path.decode("utf-8", errors="surrogateescape")
        for path in tracked_paths
        if path
        and _is_compiled_input(
            path.decode("utf-8", errors="surrogateescape")
        )
    ]
    untracked_compiled_paths = [
        path
        for code, path in status_entries
        if code == "??" and _is_compiled_input(path)
    ]
    provenance_paths = [
        "docs/turnsafe/t0_schema.md",
        "scripts/turnsafe/source_snapshot.py",
        "scripts/turnsafe/generate_build_provenance.py",
        "scripts/turnsafe/finalize_build_manifest.py",
        "scripts/turnsafe/kaist_vio_campaign.py",
        "scripts/turnsafe/test_kaist_vio_campaign.py",
        "scripts/turnsafe/event_ready_association.py",
        "scripts/turnsafe/test_event_ready_association.py",
        "scripts/turnsafe/event_ready_corpus.py",
        "scripts/turnsafe/test_event_ready_corpus.py",
        "scripts/turnsafe/baseline_digest.py",
        "scripts/turnsafe/test_baseline_digest.py",
        "scripts/turnsafe/test_source_provenance.py",
    ]
    tracked_dirty = any(code not in ("??", "OR") for code, _ in status_entries)
    untracked_provenance_paths = [
        path
        for code, path in status_entries
        if code == "??" and path in provenance_paths
    ]
    source_dirty = (
        tracked_dirty
        or bool(untracked_compiled_paths)
        or bool(untracked_provenance_paths)
    )

    snapshot: Dict[str, Any] = {
        "schema_version": SCHEMA,
        "repository": str(repository),
        "head_sha": head_sha,
        "head_tree": head_tree,
        "status_porcelain_sha256": _sha256_bytes(status_raw),
        "status_entries": [
            {"code": code, "path": path} for code, path in status_entries
        ],
        "tracked_diff_sha256": _sha256_bytes(tracked_diff),
        "tracked_diff_size_bytes": len(tracked_diff),
        "compiled_inputs": _file_records(repository, compiled_paths),
        "provenance_inputs": _file_records(repository, provenance_paths),
        "untracked_compiled_inputs": _file_records(
            repository, untracked_compiled_paths
        ),
        "untracked_provenance_inputs": _file_records(
            repository, untracked_provenance_paths
        ),
        "source_dirty": source_dirty,
        "protected_cp2_excluded": True,
    }
    snapshot["aggregate_source_snapshot_sha256"] = _sha256_bytes(
        _canonical_bytes(snapshot)
    )
    return snapshot


def write_snapshot(repository: Path, output: Path) -> Dict[str, Any]:
    snapshot = create_snapshot(repository)
    output = output.resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp.{}".format(os.getpid()))
    payload = json.dumps(snapshot, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(output))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return snapshot


def main(argv: Sequence[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: source_snapshot.py REPOSITORY OUTPUT",
            file=sys.stderr,
        )
        return 2
    try:
        write_snapshot(Path(argv[1]), Path(argv[2]))
    except (OSError, SnapshotError, ValueError) as exc:
        print("SOURCE_SNAPSHOT_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
