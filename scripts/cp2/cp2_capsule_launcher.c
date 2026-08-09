// SPDX-License-Identifier: GPL-3.0-or-later
/* Static, relocatable CP2-D capsule launcher (x86_64 Linux).
 *
 * Compile with -DCP2_ENTRY_MODULE='"module.name"'.  The launcher resolves its
 * capsule root from its held /proc/self/exe identity, establishes the frozen
 * hardware FP controls, and explicitly invokes the capsule-local ELF loader.
 * It never consults PATH, LD_LIBRARY_PATH, PYTHONPATH, or the ambient loader.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <unistd.h>

#if !defined(__x86_64__)
#error "cp2_capsule_launcher.c is frozen for x86_64"
#endif
#if !defined(CP2_ENTRY_MODULE)
#error "CP2_ENTRY_MODULE must be defined"
#endif
#if !defined(CP2_NUMPY_RUNTIME) || (CP2_NUMPY_RUNTIME != 0 && CP2_NUMPY_RUNTIME != 1)
#error "CP2_NUMPY_RUNTIME must be defined as zero or one"
#endif

#define CP2_EXPECTED_MXCSR ((uint32_t)0x00001f80u)
#define CP2_EXPECTED_X87_CW ((uint16_t)0x027fu)
#define CP2_X87_STATUS_MASK ((uint16_t)0x003fu)
#define CP2_X87_PERMITTED_STATUS ((uint16_t)0x0032u)
#define CP2_REQUIRED_SEALS                                                    \
  (F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL)

static int cp2_set_fp(void) {
  const uint32_t mxcsr = CP2_EXPECTED_MXCSR;
  const uint16_t x87_cw = CP2_EXPECTED_X87_CW;
  uint32_t observed_mxcsr;
  uint16_t observed_x87_cw;
  uint16_t observed_x87_sw;
  __asm__ volatile("ldmxcsr %0" : : "m"(mxcsr));
  __asm__ volatile("fnclex\n\tfldcw %0" : : "m"(x87_cw));
  __asm__ volatile("stmxcsr %0" : "=m"(observed_mxcsr));
  __asm__ volatile("fnstcw %0" : "=m"(observed_x87_cw));
  __asm__ volatile("fnstsw %0" : "=am"(observed_x87_sw));
  return observed_mxcsr == CP2_EXPECTED_MXCSR &&
         observed_x87_cw == CP2_EXPECTED_X87_CW &&
         (observed_x87_sw & CP2_X87_STATUS_MASK) == 0u;
}

static int cp2_check_fp(void) {
  uint32_t observed_mxcsr;
  uint16_t observed_x87_cw;
  uint16_t observed_x87_sw;
  __asm__ volatile("stmxcsr %0" : "=m"(observed_mxcsr));
  __asm__ volatile("fnstcw %0" : "=m"(observed_x87_cw));
  __asm__ volatile("fnstsw %0" : "=am"(observed_x87_sw));
  return observed_mxcsr == CP2_EXPECTED_MXCSR &&
         observed_x87_cw == CP2_EXPECTED_X87_CW &&
         (observed_x87_sw & CP2_X87_STATUS_MASK &
          ~CP2_X87_PERMITTED_STATUS) == 0u;
}

struct cp2_fixed_environment {
  const char *name;
  const char *value;
};

static const struct cp2_fixed_environment CP2_FIXED_ENVIRONMENT[] = {
    {"BLIS_NUM_THREADS", "1"},
    {"LANG", "C"},
    {"LC_ALL", "C"},
    {"MKL_DYNAMIC", "FALSE"},
    {"MKL_NUM_THREADS", "1"},
    {"NUMEXPR_NUM_THREADS", "1"},
    {"NPY_DISABLE_CPU_FEATURES", "X86_V3,X86_V4,AVX512_ICL,AVX512_SPR"},
    {"OMP_DYNAMIC", "FALSE"},
    {"OMP_NUM_THREADS", "1"},
    {"OPENBLAS_CORETYPE", "SkylakeX"},
    {"OPENBLAS_NUM_THREADS", "1"},
    {"PYTHONDONTWRITEBYTECODE", "1"},
    {"PYTHONNOUSERSITE", "1"},
    {"TZ", "UTC"},
    {"VECLIB_MAXIMUM_THREADS", "1"},
};

static const char *CP2_PRIVATE_NAMES[] = {
    "HOME", "MPLCONFIGDIR", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
};
static const char *CP2_PRIVATE_SUFFIXES[] = {
    "home", "mpl", "tmp", "xdg-cache", "xdg-config",
};

extern char **environ;

static int cp2_environment_index(const char *name, size_t length) {
  const size_t fixed_count = sizeof(CP2_FIXED_ENVIRONMENT) /
                             sizeof(CP2_FIXED_ENVIRONMENT[0]);
  const size_t private_count = sizeof(CP2_PRIVATE_NAMES) /
                               sizeof(CP2_PRIVATE_NAMES[0]);
  for (size_t index = 0; index < fixed_count; ++index) {
    if (strlen(CP2_FIXED_ENVIRONMENT[index].name) == length &&
        memcmp(name, CP2_FIXED_ENVIRONMENT[index].name, length) == 0) {
      return (int)index;
    }
  }
  for (size_t index = 0; index < private_count; ++index) {
    if (strlen(CP2_PRIVATE_NAMES[index]) == length &&
        memcmp(name, CP2_PRIVATE_NAMES[index], length) == 0) {
      return (int)(fixed_count + index);
    }
  }
  return -1;
}

static int cp2_normal_absolute_path(const char *value) {
  const size_t length = strlen(value);
  if (length < 2 || value[0] != '/' || value[length - 1] == '/') {
    return 0;
  }
  if (strstr(value, "//") != NULL || strstr(value, "/./") != NULL ||
      strstr(value, "/../") != NULL ||
      (length >= 2 && strcmp(value + length - 2, "/.") == 0) ||
      (length >= 3 && strcmp(value + length - 3, "/..") == 0)) {
    return 0;
  }
  return 1;
}

static int cp2_validate_environment(void) {
  const size_t fixed_count = sizeof(CP2_FIXED_ENVIRONMENT) /
                             sizeof(CP2_FIXED_ENVIRONMENT[0]);
  const size_t private_count = sizeof(CP2_PRIVATE_NAMES) /
                               sizeof(CP2_PRIVATE_NAMES[0]);
  const size_t total_count = fixed_count + private_count;
  unsigned char seen[32] = {0};
  const char *private_values[5] = {0};
  size_t count = 0;
  for (char **entry = environ; *entry != NULL; ++entry) {
    const char *equals = strchr(*entry, '=');
    if (equals == NULL) {
      return 0;
    }
    const int index = cp2_environment_index(*entry, (size_t)(equals - *entry));
    if (index < 0 || (size_t)index >= total_count || seen[index]) {
      return 0;
    }
    seen[index] = 1;
    ++count;
    const char *value = equals + 1;
    if ((size_t)index < fixed_count) {
      if (strcmp(value, CP2_FIXED_ENVIRONMENT[index].value) != 0) {
        return 0;
      }
    } else {
      private_values[(size_t)index - fixed_count] = value;
    }
  }
  if (count != total_count) {
    return 0;
  }
  size_t root_length = 0;
  for (size_t index = 0; index < private_count; ++index) {
    const char *value = private_values[index];
    const size_t value_length = value == NULL ? 0 : strlen(value);
    const size_t suffix_length = strlen(CP2_PRIVATE_SUFFIXES[index]);
    if (!cp2_normal_absolute_path(value) || value_length <= suffix_length + 1 ||
        value[value_length - suffix_length - 1] != '/' ||
        strcmp(value + value_length - suffix_length, CP2_PRIVATE_SUFFIXES[index]) != 0) {
      return 0;
    }
    const size_t candidate_root_length = value_length - suffix_length - 1;
    if (index == 0) {
      root_length = candidate_root_length;
    } else if (candidate_root_length != root_length ||
               memcmp(value, private_values[0], root_length) != 0) {
      return 0;
    }
  }
  return 1;
}

static int cp2_private_directory(int descriptor) {
  struct stat status;
  return fstat(descriptor, &status) == 0 && S_ISDIR(status.st_mode) &&
         status.st_uid == geteuid() && (status.st_mode & 07777) == 0700;
}

static int cp2_open_private_directory_at(int parent, const char *name) {
  const int descriptor =
      openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
  if (descriptor < 0 || !cp2_private_directory(descriptor)) {
    if (descriptor >= 0) {
      (void)close(descriptor);
    }
    return -1;
  }
  return descriptor;
}

static int cp2_open_regular_at(int parent, const char *name, mode_t mode) {
  const int descriptor = openat(parent, name, O_RDONLY | O_NOFOLLOW);
  struct stat status;
  if (descriptor < 0 || fstat(descriptor, &status) != 0 ||
      !S_ISREG(status.st_mode) || status.st_uid != geteuid() ||
      (status.st_nlink != 1 &&
       (status.st_nlink != 0 ||
        fcntl(descriptor, F_GET_SEALS) != CP2_REQUIRED_SEALS)) ||
      (status.st_mode & 07777) != mode) {
    if (descriptor >= 0) {
      (void)close(descriptor);
    }
    return -1;
  }
  return descriptor;
}

static int cp2_same_inode(int first, int second) {
  struct stat left;
  struct stat right;
  return fstat(first, &left) == 0 && fstat(second, &right) == 0 &&
         left.st_dev == right.st_dev && left.st_ino == right.st_ino;
}

static int cp2_name_still_selects(int parent, const char *name, int held) {
  const int observed = openat(parent, name, O_PATH | O_NOFOLLOW);
  if (observed < 0) {
    return 0;
  }
  const int same = cp2_same_inode(observed, held);
  (void)close(observed);
  return same;
}

static int cp2_fd_path(char *output, size_t capacity, int descriptor) {
  const int count = snprintf(output, capacity, "/proc/self/fd/%d", descriptor);
  return count >= 0 && (size_t)count < capacity;
}

int main(int argc, char **argv) {
  if (!cp2_validate_environment()) {
    fputs("CP2 capsule launcher environment differs\n", stderr);
    return 125;
  }
  char executable[PATH_MAX + 1];
  const ssize_t count = readlink("/proc/self/exe", executable, PATH_MAX);
  if (count <= 0 || count >= PATH_MAX) {
    fprintf(stderr,
            "CP2 capsule launcher cannot resolve its executable: errno=%d\n",
            errno);
    return 125;
  }
  executable[count] = '\0';
  char *bin_component = strrchr(executable, '/');
  if (bin_component == NULL || strcmp(bin_component, "/launcher") != 0) {
    fputs("CP2 capsule launcher path differs\n", stderr);
    return 125;
  }
  *bin_component = '\0';
  char *root_component = strrchr(executable, '/');
  if (root_component == NULL || strcmp(root_component, "/bin") != 0) {
    fputs("CP2 capsule launcher root differs\n", stderr);
    return 125;
  }
  *root_component = '\0';
  const char *root = executable;
  const int self_fd = open("/proc/self/exe", O_PATH);
  const int root_fd = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
  if (self_fd < 0 || root_fd < 0 || !cp2_private_directory(root_fd)) {
    fputs("CP2 capsule launcher cannot hold its private root\n", stderr);
    return 125;
  }
  const int bin_fd = cp2_open_private_directory_at(root_fd, "bin");
  const int held_launcher =
      bin_fd < 0 ? -1 : cp2_open_regular_at(bin_fd, "launcher", 0555);
  const int native_fd = cp2_open_private_directory_at(root_fd, "native");
  const int python_root_fd = cp2_open_private_directory_at(root_fd, "python");
  const int python_bin_fd = python_root_fd < 0
                                ? -1
                                : cp2_open_private_directory_at(python_root_fd, "bin");
  const int python_lib_fd = python_root_fd < 0
                                ? -1
                                : cp2_open_private_directory_at(python_root_fd, "lib");
#if CP2_NUMPY_RUNTIME
  const int python311_fd = python_lib_fd < 0
                               ? -1
                               : cp2_open_private_directory_at(python_lib_fd, "python3.11");
  const int numpy_libs_fd = python311_fd < 0
                                ? -1
                                : cp2_open_private_directory_at(python311_fd, "numpy.libs");
#endif
  const int loader_fd = native_fd < 0
                            ? -1
                            : cp2_open_regular_at(
                                  native_fd, "ld-linux-x86-64.so.2", 0555);
  const int python_fd = python_bin_fd < 0
                            ? -1
                            : cp2_open_regular_at(python_bin_fd, "python3.11", 0555);
  if (bin_fd < 0 || held_launcher < 0 || native_fd < 0 ||
      python_root_fd < 0 || python_bin_fd < 0 || python_lib_fd < 0 ||
#if CP2_NUMPY_RUNTIME
      python311_fd < 0 || numpy_libs_fd < 0 ||
#endif
      loader_fd < 0 || python_fd < 0 || !cp2_same_inode(self_fd, held_launcher)) {
    fputs("CP2 capsule launcher held identity differs\n", stderr);
    return 125;
  }
  if (!cp2_set_fp()) {
    fputs("CP2 capsule launcher cannot establish FP controls\n", stderr);
    return 125;
  }

  char loader[64];
  char python[64];
  char native_directory[64];
  char python_lib_directory[64];
#if CP2_NUMPY_RUNTIME
  char numpy_libs_directory[64];
#endif
  char library_path[4 * PATH_MAX + 4];
  if (!cp2_fd_path(loader, sizeof(loader), loader_fd) ||
      !cp2_fd_path(python, sizeof(python), python_fd) ||
      !cp2_fd_path(native_directory, sizeof(native_directory), native_fd) ||
      !cp2_fd_path(
          python_lib_directory, sizeof(python_lib_directory), python_lib_fd)
#if CP2_NUMPY_RUNTIME
      || !cp2_fd_path(
          numpy_libs_directory, sizeof(numpy_libs_directory), numpy_libs_fd)
#endif
  ) {
    fputs("CP2 capsule launcher descriptor path exceeds its bound\n", stderr);
    return 125;
  }
  const int library_count = snprintf(
      library_path, sizeof(library_path),
#if CP2_NUMPY_RUNTIME
      "%s:%s:%s", native_directory, python_lib_directory,
      numpy_libs_directory);
#else
      "%s:%s", native_directory, python_lib_directory);
#endif
  if (library_count < 0 || (size_t)library_count >= sizeof(library_path)) {
    fputs("CP2 capsule library path exceeds its bound\n", stderr);
    return 125;
  }

  /* loader, --inhibit-cache, --library-path, value, python, flags/module/args. */
  if (argc > INT_MAX - 11) {
    fputs("CP2 capsule argument count exceeds its bound\n", stderr);
    return 125;
  }
  char **child = calloc((size_t)argc + 11u, sizeof(char *));
  if (child == NULL) {
    fputs("CP2 capsule launcher cannot allocate argv\n", stderr);
    return 125;
  }
  child[0] = loader;
  child[1] = "--inhibit-cache";
  child[2] = "--library-path";
  child[3] = library_path;
  child[4] = python;
  child[5] = "-I";
  child[6] = "-S";
  child[7] = "-B";
  child[8] = "-m";
  child[9] = CP2_ENTRY_MODULE;
  for (int index = 1; index < argc; ++index) {
    child[index + 9] = argv[index];
  }
  child[argc + 9] = NULL;
  if (!cp2_check_fp()) {
    fputs("CP2 capsule launcher FP controls changed before exec\n", stderr);
    return 125;
  }
#if defined(CP2_TEST_PREEXEC_READY_FD) && defined(CP2_TEST_PREEXEC_RELEASE_FD)
  {
    char token = 'R';
    if (write(CP2_TEST_PREEXEC_READY_FD, &token, 1u) != 1 ||
        read(CP2_TEST_PREEXEC_RELEASE_FD, &token, 1u) != 1) {
      fputs("CP2 capsule test synchronization failed\n", stderr);
      return 125;
    }
  }
#endif
  if (!cp2_name_still_selects(bin_fd, "launcher", held_launcher) ||
      !cp2_name_still_selects(
          native_fd, "ld-linux-x86-64.so.2", loader_fd) ||
      !cp2_name_still_selects(python_bin_fd, "python3.11", python_fd) ||
      !cp2_private_directory(root_fd) || !cp2_private_directory(native_fd) ||
      !cp2_private_directory(python_lib_fd)) {
    fputs("CP2 capsule identity changed before exec\n", stderr);
    return 125;
  }
  (void)syscall(SYS_execveat, loader_fd, "", child, environ, AT_EMPTY_PATH);
  fprintf(stderr, "CP2 capsule launcher exec failed: errno=%d\n", errno);
  return 125;
}
