#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict postauthorization CP2 dataset-registry resolution.

This module accepts only the one verified byte buffer returned by
``ReadinessAuthorization.read_registry_once``.  It never opens the registry or
any resolved path.  Callers must import and invoke it only after the readiness
barrier; tests use synthetic in-memory YAML.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable, List, Mapping, MutableMapping, Optional, Tuple

import yaml


SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
OFFSETS_SECONDS = (40.0, 5.0, 0.0)
BAG_SHA256 = (
    "57f440ccd68ec8dc8f9461269f5909656b86198bac3adfd677b1fcc7a1428fa9",
    "c51b0064681dfb287b6653f5fd54e6c56af5d9151c866e17574fdbc527db2311",
    "6dc6192fac63dd0a05ba745548b41fe8cae14724168a98865a81d37e681bbc81",
)
GROUND_TRUTH_SHA256 = (
    "ab1579de35a047d241e2d0d1a4f4306b4fa51d99c6f11bcdebf336ab2b784df9",
    "8c7c9873f5cb102eda2b68d665134f144eed9d5778f0dbdc82bbf8e8fdbd558e",
    "6d2f961334ff3069105be0aacf118d3c7e82bf3ab0e238acd5f40f1d897573d1",
)
MAX_REGISTRY_BYTES = 16 * 1024 * 1024


class RegistryResolutionError(ValueError):
    """Raised when the authorized registry buffer is not uniquely hash-bound."""


def _fail(message: str) -> None:
    raise RegistryResolutionError(message)


@dataclass(frozen=True)
class ResolvedSequenceInput:
    sequence_index: int
    sequence_id: str
    offset_seconds: float
    bag_path: Path
    bag_sha256: str
    ground_truth_path: Path
    ground_truth_sha256: str


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> MutableMapping[Any, Any]:
    result: MutableMapping[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise RegistryResolutionError("dataset registry contains an unhashable YAML key") from exc
        if duplicate:
            _fail("dataset registry contains a duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


def _walk_mappings(
    value: Any, seen: Optional[set] = None
) -> Iterable[Mapping[str, Any]]:
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        if all(isinstance(key, str) for key in value):
            yield value
        for child in value.values():
            yield from _walk_mappings(child, seen)
    elif isinstance(value, list):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        for child in value:
            yield from _walk_mappings(child, seen)


def _walk_scalars(value: Any, seen: Optional[set] = None) -> Iterable[Any]:
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        for child in value.values():
            yield from _walk_scalars(child, seen)
    elif isinstance(value, list):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        for child in value:
            yield from _walk_scalars(child, seen)
    else:
        yield value


def _absolute_normalized_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("/")
        and not value.startswith("//")
        and value != os.path.sep
        and not any(token in value for token in ("\0", "\r", "\n", "\\"))
        and os.path.normpath(value) == value
    )


def _resolve_one(
    mappings: Tuple[Mapping[str, Any], ...],
    sequence: str,
    expected_hash: str,
    *,
    label: str,
    candidate: Any,
) -> Path:
    scoped_candidates: List[Tuple[int, str]] = []
    for mapping in mappings:
        scalars = tuple(_walk_scalars(mapping))
        if sequence not in scalars or expected_hash not in scalars:
            continue
        for scalar in scalars:
            if _absolute_normalized_path(scalar) and candidate(scalar):
                scoped_candidates.append((len(scalars), scalar))
    if not scoped_candidates:
        _fail("registry has no hash-bound absolute {} for {}".format(label, sequence))
    minimum_scope = min(size for size, _ in scoped_candidates)
    paths = {path for size, path in scoped_candidates if size == minimum_scope}
    if len(paths) != 1:
        _fail(
            "registry must resolve exactly one hash-bound absolute {} for {}".format(
                label, sequence
            )
        )
    return Path(next(iter(paths)))


def resolve_postauthorized_sequence_inputs(
    registry_bytes: bytes,
) -> Tuple[ResolvedSequenceInput, ResolvedSequenceInput, ResolvedSequenceInput]:
    """Resolve all frozen CP2 bag/GT paths without opening any returned path."""

    if not isinstance(registry_bytes, bytes) or not registry_bytes:
        _fail("verified registry buffer is empty or not bytes")
    if len(registry_bytes) > MAX_REGISTRY_BYTES:
        _fail("verified registry exceeds the postauthorization bound")
    try:
        document = registry_bytes.decode("utf-8", "strict")
        registry = yaml.load(document, Loader=_UniqueKeyLoader)
    except RegistryResolutionError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RegistryResolutionError("verified registry is not strict UTF-8 YAML") from exc
    if not isinstance(registry, Mapping):
        _fail("dataset registry root is not a mapping")

    try:
        mappings = tuple(_walk_mappings(registry))
    except RecursionError as exc:
        raise RegistryResolutionError("dataset registry nesting exceeds the parser bound") from exc
    resolved = []
    for index, (sequence, offset, bag_hash, ground_truth_hash) in enumerate(
        zip(SEQUENCES, OFFSETS_SECONDS, BAG_SHA256, GROUND_TRUTH_SHA256)
    ):
        bag = _resolve_one(
            mappings,
            sequence,
            bag_hash,
            label="bag",
            candidate=lambda path: path.endswith(".bag"),
        )
        ground_truth = _resolve_one(
            mappings,
            sequence,
            ground_truth_hash,
            label="ground truth",
            # The hash and minimum mapping scope establish the association.
            # Excluding the already-classified bag is deliberately narrower
            # than guessing a registry key or a GT filename extension.
            candidate=lambda path: not path.endswith(".bag"),
        )
        if bag == ground_truth:
            _fail("registry resolves a bag and ground truth to the same path")
        resolved.append(
            ResolvedSequenceInput(
                sequence_index=index,
                sequence_id=sequence,
                offset_seconds=offset,
                bag_path=bag,
                bag_sha256=bag_hash,
                ground_truth_path=ground_truth,
                ground_truth_sha256=ground_truth_hash,
            )
        )
    bag_paths = tuple(str(item.bag_path) for item in resolved)
    ground_truth_paths = tuple(str(item.ground_truth_path) for item in resolved)
    if len(set(bag_paths)) != len(bag_paths):
        _fail("registry maps distinct CP2 sequences to the same bag path")
    if len(set(ground_truth_paths)) != len(ground_truth_paths):
        _fail("registry maps distinct CP2 sequences to the same ground-truth path")
    if set(bag_paths).intersection(ground_truth_paths):
        _fail("registry aliases a CP2 bag and ground-truth path")
    return tuple(resolved)  # type: ignore[return-value]


__all__ = [
    "BAG_SHA256",
    "GROUND_TRUTH_SHA256",
    "MAX_REGISTRY_BYTES",
    "OFFSETS_SECONDS",
    "RegistryResolutionError",
    "ResolvedSequenceInput",
    "SEQUENCES",
    "resolve_postauthorized_sequence_inputs",
]
