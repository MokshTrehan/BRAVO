// SPDX-License-Identifier: GPL-3.0-or-later
/*
 * CP2-D descriptor-fed capsule mount-namespace launcher.
 *
 * This executable is intentionally static.  Its own image is executed through
 * one parent-held descriptor.  Every capsule/input/output object is likewise
 * supplied as an already-open descriptor and bind-mounted individually into
 * a new user, mount, network, IPC, UTS, cgroup and PID namespace.  No source
 * pathname is consulted after the parent has frozen the descriptor set.
 *
 * Exact CLI (all counts are canonical unsigned decimal):
 *
 *   sandbox --directory-count N DIR...
 *           --read-only-file-count N FD MODE DEST...
 *           --writable-file-count N FD MODE DEST...
 *           --cwd ABS -- COMMAND...
 *
 * DIR, DEST, ABS and COMMAND[0] are normalized absolute paths in the new
 * root.  Directories must be parent-before-child.  Read-only file modes are
 * 0444 or 0555; the sole writable transport mode is 0600.
 */

#define _GNU_SOURCE 1

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/capability.h>
#include <sched.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/syscall.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#if !defined(__linux__) || !defined(__x86_64__)
#error "cp2_capsule_sandbox.c is frozen for Linux x86_64"
#endif

#ifndef CLONE_NEWCGROUP
#define CLONE_NEWCGROUP 0x02000000
#endif

#define CP2_MAX_DIRECTORIES 4096u
#define CP2_MAX_FILES 4096u
#define CP2_MAX_PATH 4096u
#define CP2_ROOT "/tmp/cp2-capsule-root"
#define CP2_PAGE_BYTES 4096u
#define CP2_TMPFS_GUARD_INODES 64u
#define CP2_MAX_TMPFS_BYTES ((uint64_t)2u << 30)
#define CP2_REQUIRED_SEALS                                                    \
  (F_SEAL_WRITE | F_SEAL_GROW | F_SEAL_SHRINK | F_SEAL_SEAL)

extern char **environ;

struct cp2_file {
  int descriptor;
  mode_t mode;
  const char *destination;
  int writable;
  uint64_t capacity;
  struct stat identity;
};

static void cp2_die(const char *format, ...) {
  va_list arguments;
  fputs("CP2 capsule sandbox failed closed: ", stderr);
  va_start(arguments, format);
  vfprintf(stderr, format, arguments);
  va_end(arguments);
  fputc('\n', stderr);
  _exit(125);
}

static int cp2_same_identity(const struct stat *left,
                             const struct stat *right) {
  return left->st_dev == right->st_dev && left->st_ino == right->st_ino &&
         left->st_mode == right->st_mode && left->st_nlink == right->st_nlink &&
         left->st_uid == right->st_uid && left->st_gid == right->st_gid &&
         left->st_size == right->st_size &&
         left->st_mtim.tv_sec == right->st_mtim.tv_sec &&
         left->st_mtim.tv_nsec == right->st_mtim.tv_nsec &&
         left->st_ctim.tv_sec == right->st_ctim.tv_sec &&
         left->st_ctim.tv_nsec == right->st_ctim.tv_nsec;
}

static unsigned long cp2_decimal(const char *text, unsigned long maximum,
                                 const char *label) {
  if (text == NULL || text[0] == '\0' ||
      (text[0] == '0' && text[1] != '\0')) {
    cp2_die("%s is not canonical unsigned decimal", label);
  }
  unsigned long value = 0;
  for (const unsigned char *cursor = (const unsigned char *)text; *cursor;
       ++cursor) {
    if (*cursor < '0' || *cursor > '9' ||
        value > (maximum - (unsigned long)(*cursor - '0')) / 10u) {
      cp2_die("%s is outside its bound", label);
    }
    value = value * 10u + (unsigned long)(*cursor - '0');
  }
  return value;
}

static uint64_t cp2_round_pages(uint64_t value) {
  if (value > UINT64_MAX - (CP2_PAGE_BYTES - 1u)) {
    cp2_die("tmpfs member size overflows page rounding");
  }
  return (value + CP2_PAGE_BYTES - 1u) & ~(uint64_t)(CP2_PAGE_BYTES - 1u);
}

static uint64_t cp2_checked_add(uint64_t left, uint64_t right,
                                const char *label) {
  if (left > UINT64_MAX - right) {
    cp2_die("%s overflows", label);
  }
  return left + right;
}

static int cp2_component_character(unsigned char value) {
  return (value >= 'A' && value <= 'Z') ||
         (value >= 'a' && value <= 'z') ||
         (value >= '0' && value <= '9') || value == '_' || value == '+' ||
         value == '.' || value == '-';
}

static int cp2_normal_absolute_path(const char *path) {
  if (path == NULL || path[0] != '/' || path[1] == '\0' ||
      strlen(path) >= CP2_MAX_PATH || path[strlen(path) - 1] == '/') {
    return 0;
  }
  const unsigned char *component = (const unsigned char *)path + 1;
  size_t component_length = 0;
  for (const unsigned char *cursor = component;; ++cursor) {
    if (*cursor == '/' || *cursor == '\0') {
      if (component_length == 0 || component_length > 128u ||
          (component_length == 1u && component[0] == '.') ||
          (component_length == 2u && component[0] == '.' &&
           component[1] == '.')) {
        return 0;
      }
      if (*cursor == '\0') {
        return 1;
      }
      component = cursor + 1;
      component_length = 0;
    } else {
      if (!cp2_component_character(*cursor)) {
        return 0;
      }
      ++component_length;
    }
  }
}

static int cp2_path_under(const char *path, const char *root) {
  const size_t length = strlen(root);
  return strcmp(path, root) == 0 ||
         (strncmp(path, root, length) == 0 && path[length] == '/');
}

static const char *cp2_parent_path(const char *path, char *output,
                                   size_t capacity) {
  const char *slash = strrchr(path, '/');
  if (slash == NULL || slash == path || (size_t)(slash - path) >= capacity) {
    return NULL;
  }
  memcpy(output, path, (size_t)(slash - path));
  output[slash - path] = '\0';
  return output;
}

static int cp2_directory_seen(const char *path, const char **directories,
                              size_t count) {
  for (size_t index = 0; index < count; ++index) {
    if (strcmp(path, directories[index]) == 0) {
      return 1;
    }
  }
  return 0;
}

static int cp2_destination_seen(const char *path, const struct cp2_file *files,
                                size_t count) {
  for (size_t index = 0; index < count; ++index) {
    if (strcmp(path, files[index].destination) == 0) {
      return 1;
    }
  }
  return 0;
}

static int cp2_same_inode(const struct stat *left, const struct stat *right) {
  return left->st_dev == right->st_dev && left->st_ino == right->st_ino;
}

static void cp2_write_map(const char *path, unsigned long inside,
                          unsigned long outside) {
  char payload[128];
  const int count = snprintf(payload, sizeof(payload), "%lu %lu 1\n", inside,
                             outside);
  if (count <= 0 || (size_t)count >= sizeof(payload)) {
    cp2_die("user-namespace map exceeds its bound");
  }
  const int descriptor = open(path, O_WRONLY | O_CLOEXEC);
  if (descriptor < 0 || write(descriptor, payload, (size_t)count) != count ||
      close(descriptor) != 0) {
    cp2_die("cannot establish user-namespace map: errno=%d", errno);
  }
}

static void cp2_user_namespace(uid_t outer_uid, gid_t outer_gid) {
#if defined(CP2_TEST_FORCE_NAMESPACE_FAILURE)
  errno = EPERM;
  cp2_die("cannot create user namespace: errno=%d", errno);
#endif
  if (unshare(CLONE_NEWUSER) != 0) {
    cp2_die("cannot create user namespace: errno=%d", errno);
  }
  /* Linux changes ownership of /proc/self/{uid,gid}_map when a process is
   * nondumpable.  No sandbox filesystem exists yet, so reopen only this
   * mapping window and close it again immediately after setresuid/setresgid. */
  if (prctl(PR_SET_DUMPABLE, 1, 0, 0, 0) != 0) {
    cp2_die("cannot open the user-namespace mapping window: errno=%d", errno);
  }
  const int setgroups = open("/proc/self/setgroups", O_WRONLY | O_CLOEXEC);
  if (setgroups >= 0) {
    static const char deny[] = "deny\n";
    if (write(setgroups, deny, sizeof(deny) - 1u) !=
            (ssize_t)(sizeof(deny) - 1u) ||
        close(setgroups) != 0) {
      cp2_die("cannot deny user-namespace setgroups: errno=%d", errno);
    }
  } else if (errno != ENOENT) {
    cp2_die("cannot open user-namespace setgroups: errno=%d", errno);
  }
  cp2_write_map("/proc/self/uid_map", 0u, (unsigned long)outer_uid);
  cp2_write_map("/proc/self/gid_map", 0u, (unsigned long)outer_gid);
  if (setresgid(0, 0, 0) != 0 || setresuid(0, 0, 0) != 0 ||
      geteuid() != 0 || getegid() != 0) {
    cp2_die("cannot enter the mapped namespace identity: errno=%d", errno);
  }
}

static int cp2_open_relative_directory(int root, const char *absolute) {
  int descriptor = dup(root);
  if (descriptor < 0) {
    cp2_die("cannot duplicate sandbox root descriptor: errno=%d", errno);
  }
  const char *cursor = absolute + 1;
  while (*cursor) {
    const char *slash = strchr(cursor, '/');
    const size_t length = slash == NULL ? strlen(cursor) : (size_t)(slash - cursor);
    char component[129];
    if (length == 0 || length >= sizeof(component)) {
      cp2_die("sandbox path component exceeds its bound");
    }
    memcpy(component, cursor, length);
    component[length] = '\0';
    const int next = openat(descriptor, component,
                            O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (next < 0) {
      cp2_die("cannot open sandbox directory %s: errno=%d", absolute, errno);
    }
    close(descriptor);
    descriptor = next;
    if (slash == NULL) {
      break;
    }
    cursor = slash + 1;
  }
  return descriptor;
}

static void cp2_create_directory(int root, const char *absolute) {
  char parent[CP2_MAX_PATH];
  const char *parent_path = cp2_parent_path(absolute, parent, sizeof(parent));
  int parent_fd;
  const char *name;
  if (parent_path == NULL) {
    parent_fd = dup(root);
    name = absolute + 1;
  } else {
    parent_fd = cp2_open_relative_directory(root, parent_path);
    name = strrchr(absolute, '/') + 1;
  }
  if (parent_fd < 0 || mkdirat(parent_fd, name, 0700) != 0) {
    cp2_die("cannot create sandbox directory %s: errno=%d", absolute, errno);
  }
  const int descriptor = openat(
      parent_fd, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  struct stat status;
  if (descriptor < 0 || fstat(descriptor, &status) != 0 ||
      !S_ISDIR(status.st_mode) || status.st_uid != geteuid() ||
      (status.st_mode & 07777) != 0700) {
    cp2_die("sandbox directory identity differs: %s", absolute);
  }
  close(descriptor);
  close(parent_fd);
}

static void cp2_destination_path(char *output, size_t capacity,
                                 const char *destination) {
  const int count = snprintf(output, capacity, "%s%s", CP2_ROOT, destination);
  if (count <= 0 || (size_t)count >= capacity) {
    cp2_die("sandbox destination path exceeds its bound");
  }
}

static void cp2_materialize_file(int root, struct cp2_file *file) {
  char parent[CP2_MAX_PATH];
  const char *parent_path =
      cp2_parent_path(file->destination, parent, sizeof(parent));
  if (parent_path == NULL) {
    cp2_die("sandbox file cannot be rooted directly at slash");
  }
  const int parent_fd = cp2_open_relative_directory(root, parent_path);
  const char *name = strrchr(file->destination, '/') + 1;
  int destination_fd = openat(
      parent_fd, name,
      O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, file->mode);
  if (destination_fd < 0) {
    cp2_die("cannot create sandbox file %s: errno=%d", file->destination,
            errno);
  }
  char destination_path[CP2_MAX_PATH * 2u];
  cp2_destination_path(destination_path, sizeof(destination_path),
                       file->destination);
  if (file->writable) {
    if (fchmod(destination_fd, 0600) != 0 || fsync(destination_fd) != 0 ||
        mount(destination_path, destination_path, NULL, MS_BIND, NULL) != 0) {
      cp2_die("cannot prepare private writable file %s: errno=%d",
              file->destination, errno);
    }
  } else {
    unsigned char buffer[1u << 20];
    off_t offset = 0;
    while (offset < file->identity.st_size) {
      size_t requested = sizeof(buffer);
      if ((off_t)requested > file->identity.st_size - offset) {
        requested = (size_t)(file->identity.st_size - offset);
      }
      const ssize_t count =
          pread(file->descriptor, buffer, requested, offset);
      if (count <= 0 || (size_t)count > requested) {
        cp2_die("held file became short while copying %s", file->destination);
      }
      size_t written = 0;
      while (written < (size_t)count) {
        const ssize_t step = write(destination_fd, buffer + written,
                                   (size_t)count - written);
        if (step <= 0) {
          cp2_die("private file copy made no progress %s", file->destination);
        }
        written += (size_t)step;
      }
      offset += count;
    }
    if (fchmod(destination_fd, file->mode) != 0 || fsync(destination_fd) != 0) {
      cp2_die("cannot freeze private file mode %s: errno=%d",
              file->destination, errno);
    }
  }
  close(destination_fd);
  close(parent_fd);
}

static void cp2_compare_file_to_held(const struct cp2_file *file) {
  char destination_path[CP2_MAX_PATH * 2u];
  cp2_destination_path(destination_path, sizeof(destination_path),
                       file->destination);
  const int descriptor =
      open(destination_path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  struct stat before;
  if (descriptor < 0 || fstat(descriptor, &before) != 0 ||
      !S_ISREG(before.st_mode) || before.st_uid != geteuid() ||
      before.st_nlink != 1 || (before.st_mode & 07777) != file->mode ||
      before.st_size != file->identity.st_size) {
    cp2_die("private copied-file identity differs: %s", file->destination);
  }
  unsigned char left[1u << 20];
  unsigned char right[1u << 20];
  off_t offset = 0;
  while (offset < before.st_size) {
    size_t requested = sizeof(left);
    if ((off_t)requested > before.st_size - offset) {
      requested = (size_t)(before.st_size - offset);
    }
    const ssize_t left_count = pread(file->descriptor, left, requested, offset);
    const ssize_t right_count = pread(descriptor, right, requested, offset);
    if (left_count <= 0 || right_count != left_count ||
        memcmp(left, right, (size_t)left_count) != 0) {
      cp2_die("private copied-file bytes differ: %s", file->destination);
    }
    offset += left_count;
  }
  struct stat after;
  if (fstat(descriptor, &after) != 0 || !cp2_same_identity(&before, &after)) {
    cp2_die("private copied file changed during comparison: %s",
            file->destination);
  }
  close(descriptor);
}

static void cp2_export_output(struct cp2_file *file) {
  char destination_path[CP2_MAX_PATH * 2u];
  cp2_destination_path(destination_path, sizeof(destination_path),
                       file->destination);
  const int source = open(destination_path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
  struct stat before;
  struct stat handoff;
  if (source < 0 || fstat(source, &before) != 0 ||
      !S_ISREG(before.st_mode) || before.st_uid != geteuid() ||
      before.st_nlink != 1 || (before.st_mode & 07777) != 0444 ||
      before.st_size <= 0 || (uint64_t)before.st_size > file->capacity ||
      fstat(file->descriptor, &handoff) != 0 ||
      !S_ISREG(handoff.st_mode) || handoff.st_uid != geteuid() ||
      handoff.st_nlink != 0 || (handoff.st_mode & 07777) != 0600 ||
      handoff.st_size != 0 ||
      fcntl(file->descriptor, F_GET_SEALS) != 0) {
    cp2_die("private output/handoff identity differs: %s", file->destination);
  }
  unsigned char buffer[1u << 20];
  off_t offset = 0;
  while (offset < before.st_size) {
    size_t requested = sizeof(buffer);
    if ((off_t)requested > before.st_size - offset) {
      requested = (size_t)(before.st_size - offset);
    }
    const ssize_t count = pread(source, buffer, requested, offset);
    if (count <= 0 ||
        pwrite(file->descriptor, buffer, (size_t)count, offset) != count) {
      cp2_die("private output export made no progress: %s", file->destination);
    }
    offset += count;
  }
  if (fchmod(file->descriptor, 0444) != 0 || fsync(file->descriptor) != 0) {
    cp2_die("cannot freeze output handoff: errno=%d", errno);
  }
  struct stat after;
  struct stat exported;
  if (fstat(source, &after) != 0 || !cp2_same_identity(&before, &after) ||
      fstat(file->descriptor, &exported) != 0 || exported.st_nlink != 0 ||
      (exported.st_mode & 07777) != 0444 ||
      exported.st_size != before.st_size ||
      fcntl(file->descriptor, F_GET_SEALS) != 0) {
    cp2_die("private output changed during export");
  }
  close(source);
}

static int cp2_expected_path(const char *path, int directory,
                             const char **directories, size_t directory_count,
                             const struct cp2_file *files, size_t file_count) {
  if (directory) {
    return cp2_directory_seen(path, directories, directory_count);
  }
  return cp2_destination_seen(path, files, file_count);
}

static void cp2_verify_tree(int descriptor, const char *relative,
                            const char **directories, size_t directory_count,
                            const struct cp2_file *files, size_t file_count) {
  DIR *stream = fdopendir(dup(descriptor));
  if (stream == NULL) {
    cp2_die("cannot scan the closed sandbox namespace: errno=%d", errno);
  }
  errno = 0;
  for (struct dirent *entry = readdir(stream); entry != NULL;
       entry = readdir(stream)) {
    if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) {
      continue;
    }
    char path[CP2_MAX_PATH];
    const int count = snprintf(path, sizeof(path), "%s/%s", relative,
                               entry->d_name);
    if (count <= 0 || (size_t)count >= sizeof(path)) {
      cp2_die("sandbox namespace path exceeds its bound");
    }
    struct stat status;
    if (fstatat(descriptor, entry->d_name, &status, AT_SYMLINK_NOFOLLOW) != 0) {
      cp2_die("cannot stat sandbox namespace member: errno=%d", errno);
    }
    if (S_ISDIR(status.st_mode)) {
      if (!cp2_expected_path(path, 1, directories, directory_count, files,
                             file_count)) {
        cp2_die("unexpected sandbox directory: %s", path);
      }
      const int child = openat(descriptor, entry->d_name,
                               O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      if (child < 0) {
        cp2_die("cannot hold sandbox directory: %s", path);
      }
      cp2_verify_tree(child, path, directories, directory_count, files,
                      file_count);
      close(child);
    } else if (S_ISREG(status.st_mode)) {
      if (!cp2_expected_path(path, 0, directories, directory_count, files,
                             file_count)) {
        cp2_die("unexpected sandbox file: %s", path);
      }
    } else {
      cp2_die("sandbox contains a nonregular object: %s", path);
    }
    errno = 0;
  }
  if (errno != 0) {
    cp2_die("sandbox namespace scan failed: errno=%d", errno);
  }
  closedir(stream);
}

static void cp2_drop_capabilities(void) {
  struct __user_cap_header_struct header = {_LINUX_CAPABILITY_VERSION_3, 0};
  struct __user_cap_data_struct data[2];
  memset(data, 0, sizeof(data));
  if (syscall(SYS_capset, &header, data) != 0 ||
      prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0 ||
      prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
    cp2_die("cannot drop sandbox privileges: errno=%d", errno);
  }
}

static void cp2_child_exec(const char *cwd, char **command,
                           const struct cp2_file *files, size_t file_count,
                           int root) {
  char proc_path[CP2_MAX_PATH * 2u];
  cp2_destination_path(proc_path, sizeof(proc_path), "/proc");
  if (mount("proc", proc_path, "proc",
            MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0) {
    cp2_die("cannot mount isolated procfs: errno=%d", errno);
  }

  /* The root directory is its own mount so directory entries can be frozen
   * without making the one writable response-file submount read-only. */
  if (mount(CP2_ROOT, CP2_ROOT, NULL, MS_BIND | MS_REC, NULL) != 0 ||
      mount(NULL, CP2_ROOT, NULL,
            MS_BIND | MS_REMOUNT | MS_RDONLY | MS_NODEV | MS_NOSUID, NULL) !=
          0) {
    cp2_die("cannot freeze the sandbox root: errno=%d", errno);
  }
  struct statvfs root_filesystem;
  if (statvfs(CP2_ROOT, &root_filesystem) != 0 ||
      !(root_filesystem.f_flag & ST_RDONLY)) {
    cp2_die("sandbox root is not read-only");
  }

  const int frozen_root =
      open(CP2_ROOT, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (frozen_root < 0) {
    cp2_die("cannot reopen the frozen sandbox root: errno=%d", errno);
  }

  /* procfs is deliberately excluded from the finite pre-mount namespace
   * scan; it was empty when the parent scan ran and is kernel-populated now. */
  for (size_t index = 0; index < file_count; ++index) {
    struct stat current;
    if (fstat(files[index].descriptor, &current) != 0 ||
        !cp2_same_identity(&files[index].identity, &current)) {
      cp2_die("held source identity changed before capsule exec");
    }
    if (!files[index].writable) {
      cp2_compare_file_to_held(&files[index]);
    }
  }
  if (fchdir(frozen_root) != 0 || chroot(".") != 0 || chdir(cwd) != 0) {
    cp2_die("cannot enter the capsule root/cwd: errno=%d", errno);
  }
  for (int descriptor = 3; descriptor < 65536; ++descriptor) {
    if (descriptor != root && descriptor != frozen_root) {
      (void)close(descriptor);
    }
  }
  if (root >= 3) {
    (void)close(root);
  }
  if (frozen_root >= 3) {
    (void)close(frozen_root);
  }
  cp2_drop_capabilities();
  execve(command[0], command, environ);
  cp2_die("capsule command exec failed: errno=%d", errno);
}

static int cp2_wait_status(pid_t child) {
  int status = 0;
  for (;;) {
    const pid_t observed = waitpid(child, &status, 0);
    if (observed == child) {
      break;
    }
    if (observed < 0 && errno == EINTR) {
      continue;
    }
    cp2_die("cannot reap isolated capsule: errno=%d", errno);
  }
  if (WIFEXITED(status)) {
    return WEXITSTATUS(status);
  }
  if (WIFSIGNALED(status)) {
    const int signal_value = WTERMSIG(status);
    signal(signal_value, SIG_DFL);
    raise(signal_value);
    return 128 + signal_value;
  }
  cp2_die("isolated capsule returned an indeterminate wait status");
  return 125;
}

int main(int argc, char **argv) {
  if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
    cp2_die("cannot close the initial procfs inspection boundary: errno=%d",
            errno);
  }
  if (argc < 15 || strcmp(argv[1], "--tmpfs-size-bytes") != 0) {
    cp2_die("exact sandbox CLI differs");
  }
  size_t cursor = 2;
  const uint64_t tmpfs_size = (uint64_t)cp2_decimal(
      argv[cursor++], (unsigned long)CP2_MAX_TMPFS_BYTES, "tmpfs size");
  if (tmpfs_size == 0 || cursor >= (size_t)argc ||
      strcmp(argv[cursor++], "--directory-count") != 0) {
    cp2_die("tmpfs/directory marker differs");
  }
  const size_t directory_count =
      (size_t)cp2_decimal(argv[cursor++], CP2_MAX_DIRECTORIES,
                          "directory count");
  if (directory_count < 10u || cursor + directory_count >= (size_t)argc) {
    cp2_die("directory population differs");
  }
  const char **directories = calloc(directory_count, sizeof(*directories));
  if (directories == NULL) {
    cp2_die("cannot allocate directory inventory");
  }
  for (size_t index = 0; index < directory_count; ++index) {
    const char *path = argv[cursor++];
    if (!cp2_normal_absolute_path(path) ||
        (!cp2_path_under(path, "/capsule") &&
         !cp2_path_under(path, "/private") && strcmp(path, "/proc") != 0) ||
        cp2_directory_seen(path, directories, index)) {
      cp2_die("sandbox directory inventory differs");
    }
    char parent[CP2_MAX_PATH];
    const char *parent_path = cp2_parent_path(path, parent, sizeof(parent));
    if (parent_path != NULL &&
        !cp2_directory_seen(parent_path, directories, index)) {
      cp2_die("sandbox directories are not parent-before-child");
    }
    directories[index] = path;
  }
  const char *required_directories[] = {
      "/capsule",          "/private",       "/private/home",
      "/private/mpl",     "/private/preflight", "/private/tmp",
      "/private/work",    "/private/xdg-cache", "/private/xdg-config",
      "/proc",
  };
  for (size_t index = 0;
       index < sizeof(required_directories) / sizeof(required_directories[0]);
       ++index) {
    if (!cp2_directory_seen(required_directories[index], directories,
                            directory_count)) {
      cp2_die("required sandbox directory is absent");
    }
  }
  if (cursor >= (size_t)argc ||
      strcmp(argv[cursor++], "--read-only-file-count") != 0 ||
      cursor >= (size_t)argc) {
    cp2_die("read-only file inventory marker differs");
  }
  const size_t read_only_count =
      (size_t)cp2_decimal(argv[cursor++], CP2_MAX_FILES,
                          "read-only file count");
  if (read_only_count == 0 ||
      cursor + read_only_count * 3u >= (size_t)argc) {
    cp2_die("read-only file population differs");
  }
  struct cp2_file *files =
      calloc(read_only_count + CP2_MAX_FILES, sizeof(*files));
  if (files == NULL) {
    cp2_die("cannot allocate held-file inventory");
  }
  size_t file_count = 0;
  for (size_t index = 0; index < read_only_count; ++index) {
    struct cp2_file *file = &files[file_count++];
    file->descriptor =
        (int)cp2_decimal(argv[cursor++], (unsigned long)INT_MAX, "file descriptor");
    const char *mode = argv[cursor++];
    file->mode = strcmp(mode, "444") == 0 ? 0444 :
                 strcmp(mode, "555") == 0 ? 0555 : 0;
    file->destination = argv[cursor++];
    file->writable = 0;
    char parent[CP2_MAX_PATH];
    const char *parent_path =
        cp2_parent_path(file->destination, parent, sizeof(parent));
    if (file->descriptor < 3 || file->mode == 0 ||
        !cp2_normal_absolute_path(file->destination) ||
        parent_path == NULL ||
        !cp2_directory_seen(parent_path, directories, directory_count) ||
        cp2_destination_seen(file->destination, files, file_count - 1u) ||
        fstat(file->descriptor, &file->identity) != 0 ||
        !S_ISREG(file->identity.st_mode) ||
        (file->identity.st_mode & 07777) != file->mode ||
        file->identity.st_uid != geteuid() || file->identity.st_nlink != 0 ||
        fcntl(file->descriptor, F_GET_SEALS) != CP2_REQUIRED_SEALS) {
      cp2_die("read-only held-file identity differs");
    }
  }
  if (cursor >= (size_t)argc ||
      strcmp(argv[cursor++], "--writable-file-count") != 0 ||
      cursor >= (size_t)argc) {
    cp2_die("writable file inventory marker differs");
  }
  const size_t writable_count =
      (size_t)cp2_decimal(argv[cursor++], CP2_MAX_FILES - read_only_count,
                          "writable file count");
  if (writable_count > 1u || cursor + writable_count * 4u >= (size_t)argc) {
    cp2_die("writable file population differs");
  }
  for (size_t index = 0; index < writable_count; ++index) {
    struct cp2_file *file = &files[file_count++];
    file->descriptor =
        (int)cp2_decimal(argv[cursor++], (unsigned long)INT_MAX, "file descriptor");
    const char *mode = argv[cursor++];
    file->mode = strcmp(mode, "600") == 0 ? 0600 : 0;
    file->capacity = (uint64_t)cp2_decimal(
        argv[cursor++], (unsigned long)CP2_MAX_TMPFS_BYTES,
        "writable file capacity");
    file->destination = argv[cursor++];
    file->writable = 1;
    char parent[CP2_MAX_PATH];
    const char *parent_path =
        cp2_parent_path(file->destination, parent, sizeof(parent));
    if (file->descriptor < 3 || file->mode != 0600 || file->capacity == 0 ||
        !cp2_normal_absolute_path(file->destination) ||
        !cp2_path_under(file->destination, "/private") ||
        parent_path == NULL ||
        !cp2_directory_seen(parent_path, directories, directory_count) ||
        cp2_destination_seen(file->destination, files, file_count - 1u) ||
        fstat(file->descriptor, &file->identity) != 0 ||
        !S_ISREG(file->identity.st_mode) || file->identity.st_nlink != 0 ||
        file->identity.st_size != 0 ||
        (file->identity.st_mode & 07777) != 0600 ||
        file->identity.st_uid != geteuid()) {
      cp2_die("writable held-file identity differs");
    }
  }
  uint64_t required_tmpfs = cp2_checked_add(
      (uint64_t)directory_count, (uint64_t)file_count,
      "tmpfs inode population");
  required_tmpfs = cp2_checked_add(
      required_tmpfs, CP2_TMPFS_GUARD_INODES, "tmpfs inode population");
  required_tmpfs = cp2_round_pages(required_tmpfs);
  for (size_t index = 0; index < file_count; ++index) {
    const uint64_t payload = files[index].writable
                                 ? files[index].capacity
                                 : (uint64_t)files[index].identity.st_size;
    required_tmpfs = cp2_checked_add(
        required_tmpfs, cp2_round_pages(payload), "tmpfs payload population");
  }
  if (required_tmpfs != tmpfs_size || required_tmpfs > CP2_MAX_TMPFS_BYTES) {
    cp2_die("tmpfs size does not equal the checked payload/workspace bound");
  }
  for (size_t left = 0; left < file_count; ++left) {
    for (size_t right = left + 1u; right < file_count; ++right) {
      if (files[left].descriptor == files[right].descriptor ||
          cp2_same_inode(&files[left].identity, &files[right].identity)) {
        cp2_die("held-file descriptors/inodes are not unique");
      }
    }
  }
  if (cursor + 4u > (size_t)argc || strcmp(argv[cursor++], "--cwd") != 0) {
    cp2_die("sandbox cwd marker differs");
  }
  const char *cwd = argv[cursor++];
  if (!cp2_normal_absolute_path(cwd) ||
      !cp2_directory_seen(cwd, directories, directory_count) ||
      strcmp(argv[cursor++], "--") != 0 || cursor >= (size_t)argc ||
      !cp2_normal_absolute_path(argv[cursor]) ||
      !cp2_destination_seen(argv[cursor], files, file_count)) {
    cp2_die("sandbox cwd/command differs");
  }
  char **command = &argv[cursor];

  struct stat tmp_status;
  if (lstat("/tmp", &tmp_status) != 0 || !S_ISDIR(tmp_status.st_mode) ||
      tmp_status.st_uid != 0 || (tmp_status.st_mode & 07777) != 01777) {
    cp2_die("host temporary mount point identity differs");
  }
  const uid_t outer_uid = geteuid();
  const gid_t outer_gid = getegid();
  cp2_user_namespace(outer_uid, outer_gid);
  if (prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0) {
    cp2_die("cannot close the mapped procfs inspection boundary: errno=%d",
            errno);
  }
  const int namespace_flags =
      CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWIPC | CLONE_NEWUTS |
      CLONE_NEWCGROUP;
  if (unshare(namespace_flags) != 0) {
    cp2_die("cannot create private process namespaces: errno=%d", errno);
  }
  for (size_t index = 0; index < file_count; ++index) {
    struct stat mapped;
    if (fstat(files[index].descriptor, &mapped) != 0 ||
        mapped.st_dev != files[index].identity.st_dev ||
        mapped.st_ino != files[index].identity.st_ino ||
        mapped.st_mode != files[index].identity.st_mode ||
        mapped.st_nlink != files[index].identity.st_nlink ||
        mapped.st_gid != 0 || mapped.st_uid != 0 ||
        mapped.st_size != files[index].identity.st_size ||
        mapped.st_mtim.tv_sec != files[index].identity.st_mtim.tv_sec ||
        mapped.st_mtim.tv_nsec != files[index].identity.st_mtim.tv_nsec ||
        mapped.st_ctim.tv_sec != files[index].identity.st_ctim.tv_sec ||
        mapped.st_ctim.tv_nsec != files[index].identity.st_ctim.tv_nsec) {
      cp2_die("held-file identity changed across user-namespace mapping");
    }
    files[index].identity = mapped;
  }
  const uint64_t inode_count = cp2_checked_add(
      cp2_checked_add((uint64_t)directory_count, (uint64_t)file_count,
                      "tmpfs inode count"),
      CP2_TMPFS_GUARD_INODES, "tmpfs inode count");
  char tmpfs_options[128];
  const int option_count = snprintf(
      tmpfs_options, sizeof(tmpfs_options),
      "mode=0700,size=%llu,nr_inodes=%llu",
      (unsigned long long)tmpfs_size, (unsigned long long)inode_count);
  if (option_count <= 0 || (size_t)option_count >= sizeof(tmpfs_options)) {
    cp2_die("tmpfs mount options exceed their bound");
  }
  if (
      mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) != 0 ||
      mount("tmpfs", "/tmp", "tmpfs", MS_NODEV | MS_NOSUID,
            tmpfs_options) != 0 ||
      mkdir(CP2_ROOT, 0700) != 0) {
    cp2_die("cannot create private filesystem namespaces: errno=%d", errno);
  }
  const int root = open(CP2_ROOT,
                        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (root < 0) {
    cp2_die("cannot hold private capsule root: errno=%d", errno);
  }
  for (size_t index = 0; index < directory_count; ++index) {
    cp2_create_directory(root, directories[index]);
  }
  for (size_t index = 0; index < file_count; ++index) {
    cp2_materialize_file(root, &files[index]);
  }
  cp2_verify_tree(root, "", directories, directory_count, files, file_count);
  if (unshare(CLONE_NEWPID) != 0) {
    cp2_die("cannot create PID namespace: errno=%d", errno);
  }
  const pid_t child = fork();
  if (child < 0) {
    cp2_die("cannot fork PID-namespace init: errno=%d", errno);
  }
  if (child == 0) {
    cp2_child_exec(cwd, command, files, file_count, root);
  }
  const int result = cp2_wait_status(child);
  if (result == 0) {
    for (size_t index = 0; index < file_count; ++index) {
      if (files[index].writable) {
        cp2_export_output(&files[index]);
      }
    }
  }
  close(root);
  free(files);
  free(directories);
  return result;
}
