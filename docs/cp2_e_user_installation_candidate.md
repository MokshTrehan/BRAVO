# CP2-E held-profile launcher and sudoers installation candidate

Status: **superseded installation draft; do not run; CP2-E remains locked**

This is an exact candidate procedure, not permission to run it now.  The
2026-08-04 read-only probe found no usable noninteractive sudo rule, and the
separate isolation audit proves that the current v1 child-cpuset plan cannot
exclude ambient desktop tasks.  No launcher/profile installation, sudo use,
host mutation, registry access, recorded-input access, or timing run occurred
while preparing this revision.

The root launcher has no arguments and accepts only descriptor 3 as a non-root
`AF_UNIX/SOCK_SEQPACKET` peer.  Before Python execution it opens and retains:

1. the three root-owned Python source inodes;
2. the fourth root-owned canonical profile inode;
3. its own executing launcher inode; and
4. the fixed root-owned Python interpreter inode.

The C launcher hashes the interpreter and three sources against compiled
digests.  It also contains the profile's complete nonrecursive plan SHA-256.
The held profile codec recomputes that plan from every profile field except the
recursive helper/launcher/sudoers identity digests.  Python then reconstructs
the three-source closure from the held FDs and compares the held launcher
inode's full SHA-256 to the profile.  Compiling the full profile SHA into the
launcher would be a cryptographic fixed-point problem because the profile also
contains the launcher SHA; the two-way plan/full-launcher construction closes
the same substitution seam without pretending such a fixed point exists.

This is not a complete runtime closure: a production-like probe loaded 65
Python module files and 16 mapped runtime files beyond those inodes, and the
effective sudo binary/plugin/config/PAM/rule closure is not yet attested.  The
four-file installed-closure statement below is retained only to explain the
superseded draft; it is not an acceptable installation or source-freeze
predicate.  Do not execute any command in this document until a replacement
procedure freezes and verifies the complete Python/OS/sudo TCB.

Only `--data-free-reversibility-v1` is reachable.  It durably journals the full
prior before mutation, applies the exact held profile, validates it, restores
and byte-compares the full prior, and returns a bounded receipt.  Apply failure
is a failed feasibility result even if restoration succeeds.  Ambiguous
restoration retains the pending journal and stops.  Pending-journal recovery is
recovery-only and never retries the trial.  The formal six-run helper protocol
is not reachable from this launcher.

## Prerequisites

Do not proceed until all of the following are true:

- a reviewed canonical draft profile exists at an absolute private path and
  passes the complete profile codec;
- its production paths are exactly `/opt/schurvio-cp2e`,
  `/opt/schurvio-cp2e/bin/cp2e-helper`, and `/var/lib/schurvio-cp2e`;
- a replacement runtime manifest closes every Python/DSO/OS/sudo dependency;
- its plan digest is recomputed after every behavioral/profile edit;
- the exact reviewed bytes or commit containing
  `docs/cp2_e_offline_descendant_confinement_clarification_proposed.md` and
  its implementation have explicit human approval, and their descendant
  closure and non-gating foreign-affinity semantics pass the complete
  data-free protecting suite; and
- the source tree is the exact clean reviewed commit.

Recursive identity fields in the draft may contain syntactically valid
temporary SHA-256 values.  They are replaced exactly once after the launcher
is built; none affects the nonrecursive plan digest.

## 1. Data-free build and cycle closure

Use a new private build directory.  The following five compile definitions are
mandatory.  An empty or non-lowercase digest makes the launcher refuse.

```sh
cp2e_build_dir="$(mktemp -d /tmp/schurvio-cp2e-build.XXXXXX)"
cp2e_draft_profile="/absolute/private/cp2_timing_profile.draft.yaml"
cp2e_bound_profile="$cp2e_build_dir/cp2_timing_profile.yaml"

cp2e_helper_sha="$(sha256sum scripts/cp2/cp2_timing_privileged_helper.py | cut -d' ' -f1)"
cp2e_backend_sha="$(sha256sum scripts/cp2/cp2_timing_privileged_backend.py | cut -d' ' -f1)"
cp2e_codec_sha="$(sha256sum scripts/cp2/cp2_timing_profile.py | cut -d' ' -f1)"
cp2e_python_sha="$(sha256sum /usr/bin/python3.8 | cut -d' ' -f1)"
cp2e_plan_sha="$(/usr/bin/python3 -I -B \
  scripts/cp2/cp2_timing_production_identity.py \
  --profile-plan "$cp2e_draft_profile")"

/usr/bin/cc -std=c11 -O2 -Wall -Wextra -Werror -pedantic \
  -fstack-protector-strong -D_FORTIFY_SOURCE=2 -fPIE -pie \
  -Wl,-z,relro,-z,now \
  "-DCP2E_HELPER_SHA256=\"$cp2e_helper_sha\"" \
  "-DCP2E_BACKEND_SHA256=\"$cp2e_backend_sha\"" \
  "-DCP2E_PROFILE_CODEC_SHA256=\"$cp2e_codec_sha\"" \
  "-DCP2E_PYTHON_SHA256=\"$cp2e_python_sha\"" \
  "-DCP2E_PROFILE_PLAN_SHA256=\"$cp2e_plan_sha\"" \
  -o "$cp2e_build_dir/cp2e-helper" \
  scripts/cp2/cp2_timing_root_launcher.c
chmod 0755 "$cp2e_build_dir/cp2e-helper"

/usr/bin/python3 -I -B \
  scripts/cp2/cp2_timing_production_identity.py \
  --bind-profile "$cp2e_draft_profile" \
  "$cp2e_build_dir/cp2e-helper" \
  --output "$cp2e_bound_profile"

/usr/bin/python3 -I -B \
  scripts/cp2/cp2_timing_production_identity.py \
  --candidate "$cp2e_build_dir/cp2e-helper" \
  --profile "$cp2e_bound_profile" \
  > "$cp2e_build_dir/identity-receipt.json"
```

The binder uses held-parent, no-replace creation, file fsync, and parent fsync.
It refuses an existing output.  The final identity receipt must say
`candidate_profile_binding_valid_privileged_feasibility_unproved`; both
`profile_binding_valid` and `launcher_compiled_binding_valid` must be true.
This status deliberately does not claim host feasibility.

Run the complete data-free E suite and sudoers parser before installation:

```sh
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_controls.py
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_evidence.py
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_math.py
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_orchestration.py
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_privileged_backend.py
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_privileged_helper.py
/usr/bin/python3 -I -B scripts/cp2/tests/test_cp2_timing_root_launcher.py
/usr/sbin/visudo -c -f packaging/cp2e/schurvio-cp2e.sudoers
```

## 2. Superseded user-side root-owned installation — do not execute

These commands are retained as design history only and must not be executed.
They omit the complete runtime manifest described above.  Any replacement
still requires user administrative authority; Codex must not request or handle
a password.

```sh
sudo /usr/bin/install -d -o root -g root -m 0755 \
  /opt/schurvio-cp2e /opt/schurvio-cp2e/bin \
  /opt/schurvio-cp2e/lib /opt/schurvio-cp2e/profile
sudo /usr/bin/install -d -o root -g root -m 0700 \
  /var/lib/schurvio-cp2e
sudo /usr/bin/install -o root -g root -m 0755 \
  "$cp2e_build_dir/cp2e-helper" /opt/schurvio-cp2e/bin/cp2e-helper
sudo /usr/bin/install -o root -g root -m 0444 \
  scripts/cp2/cp2_timing_privileged_helper.py \
  /opt/schurvio-cp2e/lib/cp2_timing_privileged_helper.py
sudo /usr/bin/install -o root -g root -m 0444 \
  scripts/cp2/cp2_timing_privileged_backend.py \
  /opt/schurvio-cp2e/lib/cp2_timing_privileged_backend.py
sudo /usr/bin/install -o root -g root -m 0444 \
  scripts/cp2/cp2_timing_profile.py \
  /opt/schurvio-cp2e/lib/cp2_timing_profile.py
sudo /usr/bin/install -o root -g root -m 0444 \
  "$cp2e_bound_profile" \
  /opt/schurvio-cp2e/profile/cp2_timing_profile.yaml

sudo /usr/sbin/visudo -c -f packaging/cp2e/schurvio-cp2e.sudoers
sudo /usr/bin/install -o root -g root -m 0440 \
  packaging/cp2e/schurvio-cp2e.sudoers \
  /etc/sudoers.d/schurvio-cp2e
sudo /usr/sbin/visudo -c -f /etc/sudoers.d/schurvio-cp2e
```

Do not grant a shell, compiler, Python interpreter, `systemctl`, `modprobe`, or
generic writer in sudoers.  Do not grant launcher arguments.  Do not make the
checkout itself a sudo target.

## 3. Exact installed validation and authorized data-free trial

Require uid/gid `0/0`, link count one, exact modes, and exact receipt digests
for all installed inodes:

```sh
sha256sum \
  /opt/schurvio-cp2e/bin/cp2e-helper \
  /opt/schurvio-cp2e/lib/cp2_timing_privileged_helper.py \
  /opt/schurvio-cp2e/lib/cp2_timing_privileged_backend.py \
  /opt/schurvio-cp2e/lib/cp2_timing_profile.py \
  /opt/schurvio-cp2e/profile/cp2_timing_profile.yaml \
  /etc/sudoers.d/schurvio-cp2e
stat -c '%u %g %a %h %s %n' \
  /opt/schurvio-cp2e/bin/cp2e-helper \
  /opt/schurvio-cp2e/lib/cp2_timing_privileged_helper.py \
  /opt/schurvio-cp2e/lib/cp2_timing_privileged_backend.py \
  /opt/schurvio-cp2e/lib/cp2_timing_profile.py \
  /opt/schurvio-cp2e/profile/cp2_timing_profile.yaml \
  /var/lib/schurvio-cp2e /etc/sudoers.d/schurvio-cp2e
/usr/bin/sudo -n -l
```

Only after the isolation prerequisite and every identity check passes, the
single data-free transaction is invoked without arguments:

```sh
/usr/bin/python3 -I -B \
  scripts/cp2/cp2_timing_reversibility_client.py \
  > "$cp2e_build_dir/reversibility-receipt.json"
```

Exit `0` plus `transaction_status: passed` is required.  Exit `75` is a failed
or recovery-only trial; exit `76` is indeterminate with a retained pending
journal.  Neither may be retried or treated as passing.  Any permission,
isolation, control-effectiveness, thermal, clock, publication, or restoration
failure keeps the profile nonfinal.  Even a passing data-free receipt does not
unlock registry access, recorded timing, formal publication, or CP3.
