// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * Minimal pre-Python root launcher candidate for CP2-E.
 *
 * The installed binary has no configurable argv, path, module, service,
 * register, or control surface.  sudo preserves only descriptor 3, which must
 * already be the unprivileged client's AF_UNIX SOCK_SEQPACKET endpoint.  This
 * launcher opens the exact root-owned import closure and canonical profile
 * without following final symlinks, retains those inodes and its own
 * executable inode across exec, closes every unrelated file descriptor, and
 * starts isolated Python on the held helper inode.
 *
 * This source is a candidate only.  Nothing builds, installs, or invokes it
 * automatically.  The Python production entry point exposes only the
 * data-free reversibility transaction; formal execution remains fail-closed.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef CP2E_TRUSTED_ROOT
#define CP2E_TRUSTED_ROOT "/opt/schurvio-cp2e"
#endif
#ifndef CP2E_PYTHON
#define CP2E_PYTHON "/usr/bin/python3.8"
#endif
#ifndef CP2E_HELPER_SHA256
#define CP2E_HELPER_SHA256 ""
#endif
#ifndef CP2E_BACKEND_SHA256
#define CP2E_BACKEND_SHA256 ""
#endif
#ifndef CP2E_PROFILE_CODEC_SHA256
#define CP2E_PROFILE_CODEC_SHA256 ""
#endif
#ifndef CP2E_PYTHON_SHA256
#define CP2E_PYTHON_SHA256 ""
#endif
#ifndef CP2E_PROFILE_PLAN_SHA256
#define CP2E_PROFILE_PLAN_SHA256 ""
#endif

enum {
  kClientSocketFd = 3,
  kHelperFd = 198,
  kBackendFd = 199,
  kProfileCodecFd = 200,
  kPythonFd = 201,
  kCanonicalProfileFd = 202,
  kLauncherFd = 203,
};

struct sha256_context {
  uint32_t state[8];
  uint64_t bit_count;
  unsigned char block[64];
  size_t used;
};

static const uint32_t kSha256Round[64] = {
    0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U,
    0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
    0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U,
    0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
    0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU,
    0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
    0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U,
    0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
    0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U,
    0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
    0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U,
    0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
    0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U,
    0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
    0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U,
    0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U,
};

static uint32_t rotate_right(uint32_t value, unsigned int count) {
  return (value >> count) | (value << (32U - count));
}

static uint32_t load_be32(const unsigned char *value) {
  return ((uint32_t)value[0] << 24) | ((uint32_t)value[1] << 16) |
         ((uint32_t)value[2] << 8) | (uint32_t)value[3];
}

static void sha256_transform(struct sha256_context *context,
                             const unsigned char block[64]) {
  uint32_t words[64];
  for (size_t index = 0; index < 16; ++index) {
    words[index] = load_be32(block + index * 4);
  }
  for (size_t index = 16; index < 64; ++index) {
    const uint32_t s0 = rotate_right(words[index - 15], 7) ^
                        rotate_right(words[index - 15], 18) ^
                        (words[index - 15] >> 3);
    const uint32_t s1 = rotate_right(words[index - 2], 17) ^
                        rotate_right(words[index - 2], 19) ^
                        (words[index - 2] >> 10);
    words[index] = words[index - 16] + s0 + words[index - 7] + s1;
  }
  uint32_t a = context->state[0];
  uint32_t b = context->state[1];
  uint32_t c = context->state[2];
  uint32_t d = context->state[3];
  uint32_t e = context->state[4];
  uint32_t f = context->state[5];
  uint32_t g = context->state[6];
  uint32_t h = context->state[7];
  for (size_t index = 0; index < 64; ++index) {
    const uint32_t sum1 = rotate_right(e, 6) ^ rotate_right(e, 11) ^
                          rotate_right(e, 25);
    const uint32_t choice = (e & f) ^ ((~e) & g);
    const uint32_t temporary1 = h + sum1 + choice + kSha256Round[index] +
                                words[index];
    const uint32_t sum0 = rotate_right(a, 2) ^ rotate_right(a, 13) ^
                          rotate_right(a, 22);
    const uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
    const uint32_t temporary2 = sum0 + majority;
    h = g;
    g = f;
    f = e;
    e = d + temporary1;
    d = c;
    c = b;
    b = a;
    a = temporary1 + temporary2;
  }
  context->state[0] += a;
  context->state[1] += b;
  context->state[2] += c;
  context->state[3] += d;
  context->state[4] += e;
  context->state[5] += f;
  context->state[6] += g;
  context->state[7] += h;
}

static void sha256_initialize(struct sha256_context *context) {
  static const uint32_t initial[8] = {
      0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
      0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U,
  };
  memcpy(context->state, initial, sizeof(initial));
  context->bit_count = 0;
  context->used = 0;
}

static void sha256_update(struct sha256_context *context,
                          const unsigned char *input, size_t length) {
  context->bit_count += (uint64_t)length * 8U;
  while (length > 0) {
    size_t retained = 64 - context->used;
    if (retained > length) retained = length;
    memcpy(context->block + context->used, input, retained);
    context->used += retained;
    input += retained;
    length -= retained;
    if (context->used == 64) {
      sha256_transform(context, context->block);
      context->used = 0;
    }
  }
}

static void sha256_finish(struct sha256_context *context,
                          unsigned char digest[32]) {
  const uint64_t retained_bits = context->bit_count;
  const unsigned char marker = 0x80;
  const unsigned char zero = 0;
  sha256_update(context, &marker, 1);
  while (context->used != 56) {
    sha256_update(context, &zero, 1);
  }
  unsigned char length[8];
  for (size_t index = 0; index < 8; ++index) {
    length[7 - index] = (unsigned char)(retained_bits >> (index * 8));
  }
  sha256_update(context, length, sizeof(length));
  for (size_t index = 0; index < 8; ++index) {
    digest[index * 4] = (unsigned char)(context->state[index] >> 24);
    digest[index * 4 + 1] = (unsigned char)(context->state[index] >> 16);
    digest[index * 4 + 2] = (unsigned char)(context->state[index] >> 8);
    digest[index * 4 + 3] = (unsigned char)context->state[index];
  }
}

static _Noreturn void die(const char *message) {
  const int saved = errno;
  (void)dprintf(STDERR_FILENO, "CP2-E root launcher refused: %s (%s)\n",
                message, strerror(saved));
  _exit(78);
}

static void require_safe_stat(const struct stat *st, mode_t type,
                              const char *label) {
  if ((st->st_mode & S_IFMT) != type || st->st_uid != 0 ||
      (st->st_mode & 0022) != 0 || st->st_nlink != 1) {
    errno = EPERM;
    die(label);
  }
}

static unsigned char hex_nibble(char value) {
  if (value >= '0' && value <= '9') return (unsigned char)(value - '0');
  if (value >= 'a' && value <= 'f') {
    return (unsigned char)(value - 'a' + 10);
  }
  errno = EINVAL;
  die("compiled SHA-256 is not lowercase hexadecimal");
}

static void require_file_sha256(int fd, const char *expected,
                                const char *label) {
  if (strlen(expected) != 64) {
    errno = EINVAL;
    die("launcher was not compiled with a complete digest closure");
  }
  unsigned char expected_bytes[32];
  for (size_t index = 0; index < sizeof(expected_bytes); ++index) {
    expected_bytes[index] =
        (unsigned char)((hex_nibble(expected[index * 2]) << 4) |
                        hex_nibble(expected[index * 2 + 1]));
  }
  struct sha256_context context;
  sha256_initialize(&context);
  unsigned char buffer[32768];
  off_t offset = 0;
  for (;;) {
    const ssize_t count = pread(fd, buffer, sizeof(buffer), offset);
    if (count < 0) die(label);
    if (count == 0) break;
    sha256_update(&context, buffer, (size_t)count);
    offset += count;
  }
  unsigned char observed[32];
  sha256_finish(&context, observed);
  unsigned char difference = 0;
  for (size_t index = 0; index < sizeof(observed); ++index) {
    difference |= (unsigned char)(observed[index] ^ expected_bytes[index]);
  }
  if (difference != 0) {
    errno = EPERM;
    die(label);
  }
}

static void require_compiled_sha256(const char *value, const char *label) {
  if (strlen(value) != 64) {
    errno = EINVAL;
    die(label);
  }
  for (size_t index = 0; index < 64; ++index) {
    (void)hex_nibble(value[index]);
  }
}

static int open_directory_component(int parent, const char *name) {
  const int fd = openat(parent, name,
                        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) {
    die("trusted-root directory component open failed");
  }
  struct stat st;
  if (fstat(fd, &st) != 0) {
    die("trusted-root directory component stat failed");
  }
  if (!S_ISDIR(st.st_mode) || st.st_uid != 0 || (st.st_mode & 0022) != 0) {
    errno = EPERM;
    die("trusted-root directory ownership/mode differs");
  }
  return fd;
}

static int open_trusted_root(void) {
  if (CP2E_TRUSTED_ROOT[0] != '/' || strcmp(CP2E_TRUSTED_ROOT, "/") == 0 ||
      strstr(CP2E_TRUSTED_ROOT, "//") != NULL ||
      strstr(CP2E_TRUSTED_ROOT, "/../") != NULL ||
      strstr(CP2E_TRUSTED_ROOT, "/./") != NULL) {
    errno = EINVAL;
    die("compiled trusted root is not normalized absolute syntax");
  }
  int current = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (current < 0) {
    die("filesystem root open failed");
  }
  const char *cursor = CP2E_TRUSTED_ROOT + 1;
  while (*cursor != '\0') {
    const char *slash = strchr(cursor, '/');
    const size_t length = slash == NULL ? strlen(cursor) : (size_t)(slash - cursor);
    if (length == 0 || length > NAME_MAX) {
      errno = EINVAL;
      die("trusted-root component length is invalid");
    }
    char component[NAME_MAX + 1];
    memcpy(component, cursor, length);
    component[length] = '\0';
    const int following = open_directory_component(current, component);
    (void)close(current);
    current = following;
    cursor = slash == NULL ? cursor + length : slash + 1;
  }
  return current;
}

static int open_regular_at(int root_fd, const char *relative,
                           const char *label) {
  if (relative[0] == '/' || strstr(relative, "..") != NULL) {
    errno = EINVAL;
    die(label);
  }
  const int fd = openat(root_fd, relative, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) {
    die(label);
  }
  struct stat st;
  if (fstat(fd, &st) != 0) {
    die(label);
  }
  require_safe_stat(&st, S_IFREG, label);
  if (st.st_size <= 0 || st.st_size > 8 * 1024 * 1024) {
    errno = EFBIG;
    die(label);
  }
  return fd;
}

static void move_to_fixed_fd(int source, int target) {
  if (source != target && dup3(source, target, 0) < 0) {
    die("held closure descriptor duplication failed");
  }
  if (fcntl(target, F_SETFD, 0) != 0) {
    die("held closure descriptor could not survive exec");
  }
}

static void validate_client_socket(void) {
  struct stat st;
  if (fstat(kClientSocketFd, &st) != 0 || !S_ISSOCK(st.st_mode)) {
    errno = ENOTSOCK;
    die("descriptor 3 is not a socket");
  }
  int domain = 0;
  int type = 0;
  socklen_t width = sizeof(int);
  if (getsockopt(kClientSocketFd, SOL_SOCKET, SO_DOMAIN, &domain, &width) != 0 ||
      width != sizeof(int) || domain != AF_UNIX) {
    errno = EPROTOTYPE;
    die("descriptor 3 is not AF_UNIX");
  }
  width = sizeof(int);
  if (getsockopt(kClientSocketFd, SOL_SOCKET, SO_TYPE, &type, &width) != 0 ||
      width != sizeof(int) || type != SOCK_SEQPACKET) {
    errno = EPROTOTYPE;
    die("descriptor 3 is not SOCK_SEQPACKET");
  }
  struct ucred peer;
  width = sizeof(peer);
  if (getsockopt(kClientSocketFd, SOL_SOCKET, SO_PEERCRED, &peer, &width) != 0 ||
      width != sizeof(peer) || peer.pid <= 0 || peer.uid == 0) {
    errno = EPERM;
    die("client peer credentials are invalid");
  }
}

static int open_validated_python(void) {
  const int fd = open(CP2E_PYTHON, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) {
    die("fixed Python interpreter open failed");
  }
  struct stat st;
  if (fstat(fd, &st) != 0) {
    die("fixed Python interpreter stat failed");
  }
  require_safe_stat(&st, S_IFREG, "fixed Python interpreter metadata differs");
  if (st.st_size <= 0 || st.st_size > 64 * 1024 * 1024) {
    errno = EFBIG;
    die("fixed Python interpreter size differs");
  }
  require_file_sha256(
      fd, CP2E_PYTHON_SHA256, "fixed Python interpreter digest differs");
  return fd;
}

static int open_held_launcher(void) {
  /* /proc/self/exe is a kernel-owned magic link; the held target is checked. */
  const int fd = open("/proc/self/exe", O_RDONLY | O_CLOEXEC);
  if (fd < 0) {
    die("running launcher inode open failed");
  }
  struct stat st;
  if (fstat(fd, &st) != 0) {
    die("running launcher inode stat failed");
  }
  require_safe_stat(&st, S_IFREG, "running launcher inode metadata differs");
  if (st.st_size <= 0 || st.st_size > 64 * 1024 * 1024) {
    errno = EFBIG;
    die("running launcher inode size differs");
  }
  return fd;
}

static void close_unrelated_fds(void) {
  struct rlimit limit;
  if (getrlimit(RLIMIT_NOFILE, &limit) != 0) {
    die("RLIMIT_NOFILE query failed");
  }
  rlim_t maximum = limit.rlim_cur;
  if (maximum == RLIM_INFINITY || maximum > 1048576) {
    maximum = 1048576;
  }
  for (int fd = 4; (rlim_t)fd < maximum; ++fd) {
    if (fd == kHelperFd || fd == kBackendFd || fd == kProfileCodecFd ||
        fd == kPythonFd || fd == kCanonicalProfileFd || fd == kLauncherFd) {
      continue;
    }
    (void)close(fd);
  }
}

int main(int argc, char **argv) {
  (void)argv;
  if (argc != 1 || geteuid() != 0 || getuid() != 0) {
    errno = EPERM;
    die("launcher requires argument-free real/effective uid zero execution");
  }
  validate_client_socket();
  require_compiled_sha256(
      CP2E_PROFILE_PLAN_SHA256,
      "launcher was not compiled with one complete profile-plan digest");
  const int python_fd = open_validated_python();
  const int launcher_fd = open_held_launcher();
  const int root_fd = open_trusted_root();
  const int lib_fd = open_directory_component(root_fd, "lib");
  const int profile_directory_fd = open_directory_component(root_fd, "profile");
  const int helper_fd = open_regular_at(
      lib_fd, "cp2_timing_privileged_helper.py",
      "held helper source is unsafe");
  const int backend_fd = open_regular_at(
      lib_fd, "cp2_timing_privileged_backend.py",
      "held backend source is unsafe");
  const int profile_fd = open_regular_at(
      lib_fd, "cp2_timing_profile.py", "held profile codec is unsafe");
  const int canonical_profile_fd = open_regular_at(
      profile_directory_fd, "cp2_timing_profile.yaml",
      "held canonical profile is unsafe");
  require_file_sha256(
      helper_fd, CP2E_HELPER_SHA256, "held helper source digest differs");
  require_file_sha256(
      backend_fd, CP2E_BACKEND_SHA256, "held backend source digest differs");
  require_file_sha256(
      profile_fd, CP2E_PROFILE_CODEC_SHA256,
      "held profile codec digest differs");
  move_to_fixed_fd(helper_fd, kHelperFd);
  move_to_fixed_fd(backend_fd, kBackendFd);
  move_to_fixed_fd(profile_fd, kProfileCodecFd);
  move_to_fixed_fd(python_fd, kPythonFd);
  move_to_fixed_fd(canonical_profile_fd, kCanonicalProfileFd);
  move_to_fixed_fd(launcher_fd, kLauncherFd);
  if (helper_fd != kHelperFd) (void)close(helper_fd);
  if (backend_fd != kBackendFd) (void)close(backend_fd);
  if (profile_fd != kProfileCodecFd) (void)close(profile_fd);
  if (python_fd != kPythonFd) (void)close(python_fd);
  if (canonical_profile_fd != kCanonicalProfileFd) (void)close(canonical_profile_fd);
  if (launcher_fd != kLauncherFd) (void)close(launcher_fd);
  (void)close(profile_directory_fd);
  (void)close(lib_fd);
  (void)close(root_fd);
  close_unrelated_fds();

  if (setgroups(0, NULL) != 0) {
    die("supplementary groups could not be cleared");
  }
  if (chdir("/") != 0) {
    die("root launcher cwd setup failed");
  }
  (void)umask(0077);
  char *const child_argv[] = {
      (char *)CP2E_PYTHON, (char *)"-I", (char *)"-B", (char *)"-S",
      (char *)"/proc/self/fd/198", (char *)"--production-held-closure",
      (char *)"--data-free-reversibility-v1", NULL,
  };
  char *const child_env[] = {
      (char *)"LANG=C", (char *)"LC_ALL=C", (char *)"PATH=/usr/bin:/bin",
      (char *)"CP2E_CLIENT_SOCKET_FD=3", (char *)"CP2E_HELPER_SOURCE_FD=198",
      (char *)"CP2E_BACKEND_SOURCE_FD=199",
      (char *)"CP2E_PROFILE_CODEC_SOURCE_FD=200",
      (char *)"CP2E_CANONICAL_PROFILE_FD=202",
      (char *)"CP2E_LAUNCHER_FD=203",
      (char *)"CP2E_PROFILE_PLAN_SHA256=" CP2E_PROFILE_PLAN_SHA256, NULL,
  };
  fexecve(kPythonFd, child_argv, child_env);
  die("isolated Python exec failed");
}
