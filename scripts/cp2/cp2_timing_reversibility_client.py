#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fixed unprivileged client for the CP2-E data-free reversibility trial.

The client accepts no arguments and selects no profile, path, command, module,
service, register, or host control.  It creates the sole preserved
AF_UNIX/SOCK_SEQPACKET descriptor, remains alive while the root helper restores
its affinity/cpuset state, and prints only the helper's bounded canonical
receipt.  It cannot request formal execution.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import socket
import sys
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


MAX_RECEIPT_BYTES = 64 * 1024
HELPER_ARGV = (
    "/usr/bin/sudo", "-n", "-C", "4", "--",
    "/opt/schurvio-cp2e/bin/cp2e-helper",
)
HELPER_ENV = {
    "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin",
}
STATUS_EXIT = {
    "passed": 0,
    "failed_but_exactly_restored": 75,
    "recovered_prior_only_no_retry": 75,
    "indeterminate_pending_journal_retained": 76,
}


class ReversibilityClientError(RuntimeError):
    """The fixed launcher or returned receipt violated the client contract."""


def _pairs(items: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in items:
        if key in value:
            raise ReversibilityClientError("helper receipt has a duplicate key")
        value[key] = item
    return value


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8") + b"\n"


def validate_receipt_bytes(payload: bytes) -> Mapping[str, Any]:
    if (
        type(payload) is not bytes or not payload
        or len(payload) > MAX_RECEIPT_BYTES or not payload.endswith(b"\n")
        or b"\x00" in payload
    ):
        raise ReversibilityClientError("helper receipt framing differs")
    try:
        value = json.loads(
            payload.decode("utf-8", "strict"), object_pairs_hook=_pairs,
            parse_float=lambda _token: (_ for _ in ()).throw(
                ReversibilityClientError("helper receipt contains a float")
            ),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ReversibilityClientError("helper receipt contains " + token)
            ),
        )
    except ReversibilityClientError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReversibilityClientError("helper receipt is not strict JSON") from exc
    if type(value) is not dict or _canonical(value) != payload:
        raise ReversibilityClientError("helper receipt is not canonical JSON")
    status = value.get("transaction_status")
    if (
        value.get("checkpoint") != "CP2-E"
        or value.get("formal_execution_locked") is not True
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
        or status not in STATUS_EXIT
    ):
        raise ReversibilityClientError("helper receipt identity/status differs")
    expected_type = {
        "passed": "cp2e_data_free_reversibility_receipt",
        "failed_but_exactly_restored": "cp2e_data_free_reversibility_receipt",
        "recovered_prior_only_no_retry": "cp2e_data_free_recovery_receipt",
        "indeterminate_pending_journal_retained":
            "cp2e_data_free_reversibility_indeterminate",
    }[status]
    if value.get("record_type") != expected_type:
        raise ReversibilityClientError("helper receipt type/status join differs")
    return value


def _child_exec(server: socket.socket, client: socket.socket) -> None:
    try:
        client.close()
        descriptor = server.fileno()
        if descriptor != 3:
            os.dup2(descriptor, 3, inheritable=True)
            server.close()
        else:
            os.set_inheritable(3, True)
        soft_limit, _hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        maximum = 1_048_576 if soft_limit == resource.RLIM_INFINITY else min(
            int(soft_limit), 1_048_576,
        )
        os.closerange(4, maximum)
        os.execve(HELPER_ARGV[0], HELPER_ARGV, HELPER_ENV)
    except BaseException as exc:
        os.write(2, ("CP2-E reversibility client exec failed: " + str(exc) + "\n").encode(
            "utf-8", "replace",
        ))
        os._exit(78)


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments or os.getuid() == 0 or os.geteuid() == 0:
        raise ReversibilityClientError(
            "data-free client requires argument-free unprivileged execution"
        )
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    child = os.fork()
    if child == 0:  # pragma: no cover - requires installed root policy
        _child_exec(server, client)
    server.close()
    interrupted = {"value": False}
    previous = {}

    def retain_until_restored(_signum: int, _frame: Any) -> None:
        interrupted["value"] = True

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, retain_until_restored)
    payload = b""
    flags = 0
    ancillary = []
    wait_status = None
    try:
        payload, ancillary, flags, _address = client.recvmsg(
            MAX_RECEIPT_BYTES + 1, socket.CMSG_SPACE(16 * 4),
        )
        _pid, wait_status = os.waitpid(child, 0)
    finally:
        client.close()
        if wait_status is None:
            try:
                _pid, wait_status = os.waitpid(child, 0)
            except ChildProcessError:
                pass
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    if flags & (getattr(socket, "MSG_TRUNC", 0) | getattr(socket, "MSG_CTRUNC", 0)):
        raise ReversibilityClientError("helper receipt was truncated")
    if ancillary:
        raise ReversibilityClientError("helper transferred forbidden ancillary data")
    receipt = validate_receipt_bytes(payload)
    expected_exit = STATUS_EXIT[receipt["transaction_status"]]
    if (
        wait_status is None or not os.WIFEXITED(wait_status)
        or os.WEXITSTATUS(wait_status) != expected_exit
    ):
        raise ReversibilityClientError("helper exit status differs from receipt")
    os.write(sys.stdout.fileno(), payload)
    return 130 if interrupted["value"] else expected_exit


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ReversibilityClientError", "validate_receipt_bytes"]
