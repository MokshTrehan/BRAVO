#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic descriptor-identity protection for the CP2-D launcher."""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
import stat
import subprocess
import sys
import sysconfig
import tempfile
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_capsule as capsule  # noqa: E402


class LauncherIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cp2-launcher-protection-", dir="/tmp"
        )
        self.base = Path(self.temporary.name)
        os.chmod(self.base, 0o700)
        self.root = self.base / "capsule"
        for relative in (
            "",
            "bin",
            "native",
            "python",
            "python/bin",
            "python/lib",
        ):
            path = self.root / relative
            path.mkdir(exist_ok=True)
            os.chmod(path, 0o700)
        self.private = self.base / "private"
        self.private.mkdir()
        os.chmod(self.private, 0o700)
        for suffix in capsule.PRIVATE_PATH_ENVIRONMENT.values():
            name = suffix.rsplit("/", 1)[-1]
            path = self.private / name
            path.mkdir()
            os.chmod(path, 0o700)
        self.fake_loader_source = self.base / "fake-loader.c"
        self.fake_loader_source.write_text(
            "int main(void) { return 0; }\n", encoding="ascii"
        )
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-static",
                str(self.fake_loader_source),
                "-o",
                str(self.root / "native/ld-linux-x86-64.so.2"),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.chmod(self.root / "native/ld-linux-x86-64.so.2", 0o555)
        (self.root / "python/bin/python3.11").write_bytes(b"synthetic python\n")
        os.chmod(self.root / "python/bin/python3.11", 0o555)
        (self.root / "python/lib/placeholder.py").write_bytes(b"# synthetic\n")
        os.chmod(self.root / "python/lib/placeholder.py", 0o444)
        self._compile_launcher()
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-static",
                str(CP2_DIRECTORY / "cp2_capsule_sandbox.c"),
                "-o",
                str(self.root / "bin/sandbox"),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.chmod(self.root / "bin/sandbox", 0o555)
        writer_source = self.base / "writer.c"
        writer_source.write_text(
            """
#define _GNU_SOURCE 1
#include <fcntl.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
int main(int argc, char **argv) {
  static const char payload[] = "sealed-output\\n";
  if (argc != 2) return 2;
  int fd = open(argv[1], O_WRONLY | O_NOFOLLOW);
  if (fd < 0 || write(fd, payload, sizeof(payload) - 1) !=
                    (ssize_t)(sizeof(payload) - 1) ||
      fchmod(fd, 0444) != 0 || fsync(fd) != 0 || close(fd) != 0) return 3;
  return 0;
}
""".lstrip(),
            encoding="ascii",
        )
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-static",
                str(writer_source),
                "-o",
                str(self.root / "bin/writer"),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.chmod(self.root / "bin/writer", 0o555)

    def tearDown(self):
        self.temporary.cleanup()

    def _compile_launcher(self, *, ready_fd=None, release_fd=None):
        command = [
            "/usr/bin/gcc",
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-static",
            '-DCP2_ENTRY_MODULE="unused"',
            "-DCP2_NUMPY_RUNTIME=0",
        ]
        if ready_fd is not None and release_fd is not None:
            command.extend(
                [
                    "-DCP2_TEST_PREEXEC_READY_FD={}".format(ready_fd),
                    "-DCP2_TEST_PREEXEC_RELEASE_FD={}".format(release_fd),
                ]
            )
        command.extend(
            [
                str(CP2_DIRECTORY / "cp2_capsule_launcher.c"),
                "-o",
                str(self.root / "bin/launcher"),
            ]
        )
        subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.chmod(self.root / "bin/launcher", 0o555)

    def _environment(self):
        result = {}
        for name, value in capsule.EXACT_ENVIRONMENT.items():
            if value.startswith("${PRIVATE_ROOT}/"):
                result[name] = str(self.private / value.rsplit("/", 1)[-1])
            else:
                result[name] = value
        return result

    def _run(self):
        return subprocess.run(
            [str(self.root / "bin/launcher"), "--version"],
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def _staged(self):
        rows = (
            ("bin/launcher", "capsule_launcher", 0o555),
            ("bin/sandbox", "capsule_sandbox", 0o555),
            ("bin/writer", "capsule_launcher", 0o555),
            ("native/ld-linux-x86-64.so.2", "native_loader", 0o555),
            ("python/bin/python3.11", "python_interpreter", 0o555),
            ("python/lib/placeholder.py", "python_stdlib", 0o444),
        )
        entries = []
        for relative, role, mode in rows:
            payload = (self.root / relative).read_bytes()
            entries.append(
                capsule.CapsuleEntry(
                    relative,
                    role,
                    mode,
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                )
            )
        return capsule.StagedCapsule(
            self.root,
            "synthetic-profile",
            "0" * 64,
            "direct_math",
            "x86_64",
            "1" * 64,
            capsule.inventory_sha256(entries),
            "2" * 64,
            tuple(entries),
        )

    def _sandbox_environment(self):
        result = {}
        for name, value in capsule.EXACT_ENVIRONMENT.items():
            result[name] = (
                "/private/" + value.rsplit("/", 1)[-1]
                if value.startswith("${PRIVATE_ROOT}/")
                else value
            )
        return result

    def test_descriptor_held_launcher_executes_with_exact_private_tree(self):
        completed = self._run()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

        # Protect the second x86 floating-point exception domain as executable
        # behavior, not merely as source text.  A masked x87 0/0 sets IE in the
        # calling thread; the capsule-native module must reject it even though
        # MXCSR and the x87 control word remain unchanged.
        extension = self.base / (
            "cp2_fp_control" + str(sysconfig.get_config_var("EXT_SUFFIX"))
        )
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                "-shared",
                "-I" + sysconfig.get_paths()["include"],
                str(CP2_DIRECTORY / "cp2_fp_control_module.c"),
                "-o",
                str(extension),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        helper_source = self.base / "x87-invalid.c"
        helper_source.write_text(
            """
void cp2_raise_x87_invalid(void) {
  __asm__ volatile("fldz\\n\\tfldz\\n\\tfdivp %%st, %%st(1)\\n\\tfstp %%st(0)"
                   : : : "st");
}
""".lstrip(),
            encoding="ascii",
        )
        helper = self.base / "x87-invalid.so"
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                "-shared",
                str(helper_source),
                "-o",
                str(helper),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        probe = self.base / "libcp2-fp-probe.so"
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fPIC",
                "-shared",
                str(CP2_DIRECTORY / "cp2_fp_control.c"),
                "-o",
                str(probe),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        x87_check = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                (
                    "import ctypes,importlib.util,sys;"
                    "s=importlib.util.spec_from_file_location('cp2_fp_control',sys.argv[1]);"
                    "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                    "m.establish();h=ctypes.CDLL(sys.argv[2]);"
                    "h.cp2_raise_x87_invalid();"
                    "\ntry:m.verify()\n"
                    "except RuntimeError as e:\n"
                    " assert 'X87_SW=0x0001' in str(e)\n"
                    "else:raise AssertionError('x87 invalid status was accepted')\n"
                    "m.establish();p=ctypes.CDLL(sys.argv[3]);"
                    "u32=ctypes.c_uint32();u16=ctypes.c_uint16();sw=ctypes.c_uint16();"
                    "args=(ctypes.POINTER(ctypes.c_uint32),ctypes.POINTER(ctypes.c_uint16),ctypes.POINTER(ctypes.c_uint16));"
                    "p.cp2_fp_establish.argtypes=args;p.cp2_fp_verify.argtypes=args;"
                    "assert p.cp2_fp_establish(ctypes.byref(u32),ctypes.byref(u16),ctypes.byref(sw))==0;"
                    "h.cp2_raise_x87_invalid();"
                    "assert p.cp2_fp_verify(ctypes.byref(u32),ctypes.byref(u16),ctypes.byref(sw))==1;"
                    "assert sw.value&1"
                ),
                str(extension),
                str(helper),
                str(probe),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(x87_check.returncode, 0, x87_check.stderr)

    def test_root_mode_hardlink_and_symlink_substitutions_fail_closed(self):
        os.chmod(self.root, 0o755)
        self.assertEqual(self._run().returncode, 125)
        os.chmod(self.root, 0o700)

        hardlink = self.root / "bin/launcher-second-link"
        os.link(self.root / "bin/launcher", hardlink)
        self.assertEqual(self._run().returncode, 125)
        hardlink.unlink()

        python = self.root / "python/bin/python3.11"
        python.unlink()
        python.symlink_to(self.root / "native/ld-linux-x86-64.so.2")
        self.assertEqual(self._run().returncode, 125)

    def test_coordinated_python_name_replacement_after_hold_is_rejected(self):
        ready_read, ready_write = os.pipe()
        release_read, release_write = os.pipe()
        try:
            self._compile_launcher(ready_fd=ready_write, release_fd=release_read)
            process = subprocess.Popen(
                [str(self.root / "bin/launcher"), "--version"],
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(ready_write, release_read),
            )
            os.close(ready_write)
            ready_write = -1
            os.close(release_read)
            release_read = -1
            self.assertEqual(os.read(ready_read, 1), b"R")
            python = self.root / "python/bin/python3.11"
            held = self.root / "python/bin/python3.11-held"
            python.rename(held)
            python.write_bytes(b"coordinated same-mode replacement\n")
            os.chmod(python, 0o555)
            os.write(release_write, b"C")
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 125)
            self.assertEqual(stdout, b"")
            self.assertIn(b"identity changed before exec", stderr)
            self.assertTrue(stat.S_ISREG(held.stat().st_mode))
        finally:
            for descriptor in (ready_read, ready_write, release_read, release_write):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def test_sealed_memfd_namespace_executes_without_ambient_capsule_paths(self):
        with capsule.SealedCapsuleSandbox(self._staged()) as sealed:
            with sealed.invocation(("/capsule/bin/launcher", "--version")) as invocation:
                completed = subprocess.run(
                    invocation.argv,
                    executable=invocation.executable,
                    cwd="/tmp",
                    env=self._sandbox_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=invocation.pass_fds,
                    timeout=20,
                    check=False,
                )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_sealed_memfd_namespace_survives_path_swap_then_restore(self):
        staged = self._staged()
        with capsule.SealedCapsuleSandbox(staged) as sealed:
            held = []
            for entry in staged.entries:
                path = self.root / entry.path
                old = path.with_name(path.name + ".held")
                path.rename(old)
                path.write_bytes(b"coordinated replacement for " + entry.path.encode("ascii"))
                os.chmod(path, entry.mode)
                held.append((path, old))
            try:
                with sealed.invocation(("/capsule/bin/launcher", "--version")) as invocation:
                    completed = subprocess.run(
                        invocation.argv,
                        executable=invocation.executable,
                        cwd="/tmp",
                        env=self._sandbox_environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        pass_fds=invocation.pass_fds,
                        timeout=20,
                        check=False,
                    )
            finally:
                for path, old in held:
                    path.unlink()
                    old.rename(path)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_writable_output_is_exported_to_then_sealed_on_parent_memfd(self):
        with capsule.SealedCapsuleSandbox(self._staged()) as sealed:
            with sealed.invocation(
                ("/capsule/bin/writer", "/private/work/response.json"),
                read_only_inputs={
                    "/private/work/over-four-mib.input": b"x" * ((5 << 20) + 1)
                },
                writable_outputs={"/private/work/response.json": 1024},
            ) as invocation:
                completed = subprocess.run(
                    invocation.argv,
                    executable=invocation.executable,
                    cwd="/tmp",
                    env=self._sandbox_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=invocation.pass_fds,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                output = invocation.seal_output(
                    "/private/work/response.json", 1024
                )
        self.assertEqual(output, b"sealed-output\n")
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(completed.stderr, b"")

    def test_namespace_unavailability_fails_closed_before_capsule_exec(self):
        sandbox = self.root / "bin/sandbox"
        subprocess.run(
            [
                "/usr/bin/gcc",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-static",
                "-DCP2_TEST_FORCE_NAMESPACE_FAILURE=1",
                str(CP2_DIRECTORY / "cp2_capsule_sandbox.c"),
                "-o",
                str(sandbox),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.chmod(sandbox, 0o555)
        with capsule.SealedCapsuleSandbox(self._staged()) as sealed:
            with sealed.invocation(("/capsule/bin/launcher", "--version")) as invocation:
                completed = subprocess.run(
                    invocation.argv,
                    executable=invocation.executable,
                    cwd="/tmp",
                    env=self._sandbox_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=invocation.pass_fds,
                    timeout=20,
                    check=False,
                )
        self.assertEqual(completed.returncode, 125)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"cannot create user namespace", completed.stderr)


if __name__ == "__main__":
    unittest.main()
