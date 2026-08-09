// SPDX-License-Identifier: GPL-3.0-or-later
/* CPython binding for the frozen CP2-D x86_64 FP control probe.
 *
 * This extension intentionally has no dependency beyond CPython and libc.
 */

#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <cpuid.h>
#include <dlfcn.h>
#include <stdint.h>
#include <string.h>

#if !defined(__x86_64__)
#error "cp2_fp_control_module.c is frozen for x86_64"
#endif

#define CP2_EXPECTED_MXCSR ((uint32_t)0x00001f80u)
#define CP2_MXCSR_CONTROL_MASK ((uint32_t)0x0000ffc0u)
#define CP2_MXCSR_STATUS_MASK ((uint32_t)0x0000003fu)
#define CP2_MXCSR_PERMITTED_STATUS ((uint32_t)0x00000032u)
#define CP2_EXPECTED_X87_CW ((uint16_t)0x027fu)
#define CP2_X87_STATUS_MASK ((uint16_t)0x003fu)
#define CP2_X87_PERMITTED_STATUS ((uint16_t)0x0032u)

static void cp2_read(uint32_t *mxcsr, uint16_t *x87_cw, uint16_t *x87_sw) {
  __asm__ volatile("stmxcsr %0" : "=m"(*mxcsr));
  __asm__ volatile("fnstcw %0" : "=m"(*x87_cw));
  __asm__ volatile("fnstsw %0" : "=am"(*x87_sw));
}

static PyObject *cp2_tuple_or_error(uint32_t mxcsr, uint16_t x87_cw,
                                    uint16_t x87_sw) {
  if ((mxcsr & CP2_MXCSR_CONTROL_MASK) != CP2_EXPECTED_MXCSR ||
      x87_cw != CP2_EXPECTED_X87_CW ||
      (mxcsr & CP2_MXCSR_STATUS_MASK &
       ~CP2_MXCSR_PERMITTED_STATUS) != 0u ||
      (x87_sw & CP2_X87_STATUS_MASK &
       ~CP2_X87_PERMITTED_STATUS) != 0u) {
    PyErr_Format(PyExc_RuntimeError,
                 "floating-point controls differ: MXCSR=0x%08x, X87_CW=0x%04x, X87_SW=0x%04x",
                 (unsigned int)mxcsr, (unsigned int)x87_cw,
                 (unsigned int)x87_sw);
    return NULL;
  }
  return Py_BuildValue("(IH)", (unsigned int)mxcsr, (unsigned int)x87_cw);
}

static PyObject *cp2_establish(PyObject *self, PyObject *ignored) {
  const uint32_t expected_mxcsr = CP2_EXPECTED_MXCSR;
  const uint16_t expected_x87_cw = CP2_EXPECTED_X87_CW;
  uint32_t mxcsr;
  uint16_t x87_cw;
  uint16_t x87_sw;
  (void)self;
  (void)ignored;
  __asm__ volatile("ldmxcsr %0" : : "m"(expected_mxcsr));
  __asm__ volatile("fnclex\n\tfldcw %0" : : "m"(expected_x87_cw));
  cp2_read(&mxcsr, &x87_cw, &x87_sw);
  if ((x87_sw & CP2_X87_STATUS_MASK) != 0u) {
    PyErr_Format(PyExc_RuntimeError,
                 "floating-point establish retained X87_SW=0x%04x",
                 (unsigned int)x87_sw);
    return NULL;
  }
  return cp2_tuple_or_error(mxcsr, x87_cw, x87_sw);
}

static PyObject *cp2_verify(PyObject *self, PyObject *ignored) {
  uint32_t mxcsr;
  uint16_t x87_cw;
  uint16_t x87_sw;
  (void)self;
  (void)ignored;
  cp2_read(&mxcsr, &x87_cw, &x87_sw);
  PyObject *result = cp2_tuple_or_error(mxcsr, x87_cw, x87_sw);
  if (result == NULL) {
    return NULL;
  }
  /* Clear permitted sticky observations only after rejecting fatal status. */
  const uint32_t normalized_mxcsr = mxcsr & CP2_MXCSR_CONTROL_MASK;
  __asm__ volatile("ldmxcsr %0" : : "m"(normalized_mxcsr));
  __asm__ volatile("fnclex");
  return result;
}

static PyObject *cp2_openblas_threads(PyObject *self, PyObject *arguments) {
  typedef int (*cp2_thread_query)(void);
  union {
    void *object;
    cp2_thread_query function;
  } symbol;
  const char *path = NULL;
  (void)self;
  if (!PyArg_ParseTuple(arguments, "s:openblas_threads", &path)) {
    return NULL;
  }
  void *handle = dlopen(path, RTLD_LAZY | RTLD_NOLOAD);
  if (handle == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "selected NumPy OpenBLAS library is not already loaded");
    return NULL;
  }
  (void)dlerror();
  symbol.object = dlsym(handle, "scipy_openblas_get_num_threads64_");
  const char *error = dlerror();
  if (error != NULL || symbol.object == NULL) {
    (void)dlclose(handle);
    PyErr_SetString(PyExc_RuntimeError,
                    "selected NumPy OpenBLAS thread-query symbol is absent");
    return NULL;
  }
  const int count = symbol.function();
  (void)dlclose(handle);
  if (count <= 0) {
    PyErr_SetString(PyExc_RuntimeError,
                    "selected NumPy OpenBLAS returned an invalid thread count");
    return NULL;
  }
  return PyLong_FromLong((long)count);
}

static void *cp2_required_symbol(void *handle, const char *name) {
  (void)dlerror();
  void *symbol = dlsym(handle, name);
  const char *error = dlerror();
  if (error != NULL || symbol == NULL) {
    PyErr_Format(PyExc_RuntimeError,
                 "selected NumPy OpenBLAS symbol is absent: %s", name);
    return NULL;
  }
  return symbol;
}

static PyObject *cp2_openblas_identity(PyObject *self, PyObject *arguments) {
  typedef int (*cp2_int_query)(void);
  typedef const char *(*cp2_text_query)(void);
  union {
    void *object;
    cp2_int_query integer;
    cp2_text_query text;
  } threads_symbol, procs_symbol, parallel_symbol, config_symbol, core_symbol;
  const char *path = NULL;
  (void)self;
  if (!PyArg_ParseTuple(arguments, "s:openblas_identity", &path)) {
    return NULL;
  }
  void *handle = dlopen(path, RTLD_LAZY | RTLD_NOLOAD);
  if (handle == NULL) {
    PyErr_SetString(PyExc_RuntimeError,
                    "selected NumPy OpenBLAS library is not already loaded");
    return NULL;
  }
  threads_symbol.object =
      cp2_required_symbol(handle, "scipy_openblas_get_num_threads64_");
  procs_symbol.object =
      cp2_required_symbol(handle, "scipy_openblas_get_num_procs64_");
  parallel_symbol.object =
      cp2_required_symbol(handle, "scipy_openblas_get_parallel64_");
  config_symbol.object =
      cp2_required_symbol(handle, "scipy_openblas_get_config64_");
  core_symbol.object =
      cp2_required_symbol(handle, "scipy_openblas_get_corename64_");
  if (threads_symbol.object == NULL || procs_symbol.object == NULL ||
      parallel_symbol.object == NULL || config_symbol.object == NULL ||
      core_symbol.object == NULL) {
    (void)dlclose(handle);
    return NULL;
  }
  const int threads = threads_symbol.integer();
  const int procs = procs_symbol.integer();
  const int parallel = parallel_symbol.integer();
  const char *config = config_symbol.text();
  const char *core = core_symbol.text();
  if (threads <= 0 || procs <= 0 || parallel < 0 || config == NULL ||
      core == NULL || strlen(config) > 4096u || strlen(core) > 256u) {
    (void)dlclose(handle);
    PyErr_SetString(PyExc_RuntimeError,
                    "selected NumPy OpenBLAS returned an invalid identity");
    return NULL;
  }
  PyObject *result = Py_BuildValue(
      "{s:i,s:i,s:i,s:s,s:s}", "threads", threads, "num_procs", procs,
      "parallel", parallel, "config", config, "corename", core);
  (void)dlclose(handle);
  return result;
}

static PyObject *cp2_x86_cpuid_identity(PyObject *self, PyObject *ignored) {
  unsigned int maximum;
  unsigned int eax = 0, ebx = 0, ecx = 0, edx = 0;
  unsigned int leaf1_eax, leaf1_ecx, leaf1_edx;
  unsigned int leaf7_ebx, leaf7_ecx, leaf7_edx;
  unsigned int ext1_ecx = 0, ext1_edx = 0;
  uint64_t xcr0 = 0;
  char vendor[13];
  (void)self;
  (void)ignored;
  maximum = __get_cpuid_max(0, NULL);
  if (maximum < 7u) {
    PyErr_SetString(PyExc_RuntimeError, "x86 CPUID basic leaves are absent");
    return NULL;
  }
  __cpuid(0, eax, ebx, ecx, edx);
  memcpy(vendor, &ebx, 4u);
  memcpy(vendor + 4, &edx, 4u);
  memcpy(vendor + 8, &ecx, 4u);
  vendor[12] = '\0';
  __cpuid(1, eax, ebx, ecx, edx);
  leaf1_eax = eax;
  leaf1_ecx = ecx;
  leaf1_edx = edx;
  __cpuid_count(7, 0, eax, ebx, ecx, edx);
  leaf7_ebx = ebx;
  leaf7_ecx = ecx;
  leaf7_edx = edx;
  if (__get_cpuid_max(0x80000000u, NULL) >= 0x80000001u) {
    __cpuid(0x80000001u, eax, ebx, ecx, edx);
    ext1_ecx = ecx;
    ext1_edx = edx;
  }
  if ((leaf1_ecx & (1u << 27)) != 0u) {
    unsigned int xcr0_low, xcr0_high;
    __asm__ volatile("xgetbv" : "=a"(xcr0_low), "=d"(xcr0_high) : "c"(0));
    xcr0 = ((uint64_t)xcr0_high << 32) | (uint64_t)xcr0_low;
  }
  unsigned int base_family = (leaf1_eax >> 8) & 0x0fu;
  unsigned int base_model = (leaf1_eax >> 4) & 0x0fu;
  unsigned int family = base_family;
  unsigned int model = base_model;
  if (base_family == 0x0fu) {
    family += (leaf1_eax >> 20) & 0xffu;
  }
  if (base_family == 0x06u || base_family == 0x0fu) {
    model += ((leaf1_eax >> 16) & 0x0fu) << 4;
  }
  return Py_BuildValue(
      "{s:s,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:I,s:K}",
      "vendor", vendor, "family", family, "model", model, "stepping",
      leaf1_eax & 0x0fu, "leaf1_eax", leaf1_eax, "leaf1_ecx", leaf1_ecx,
      "leaf1_edx", leaf1_edx, "leaf7_ebx", leaf7_ebx, "leaf7_ecx",
      leaf7_ecx, "leaf7_edx", leaf7_edx, "extended_leaf1_ecx", ext1_ecx,
      "extended_leaf1_edx", ext1_edx, "xcr0", (unsigned long long)xcr0);
}

static PyMethodDef cp2_methods[] = {
    {"establish", cp2_establish, METH_NOARGS,
     "Set and verify MXCSR=0x1f80 and X87_CW=0x027f."},
    {"verify", cp2_verify, METH_NOARGS,
     "Verify frozen controls and clear only sticky status bits."},
    {"openblas_threads", cp2_openblas_threads, METH_VARARGS,
     "Return the selected NumPy OpenBLAS backend's live thread count."},
    {"openblas_identity", cp2_openblas_identity, METH_VARARGS,
     "Return the selected NumPy OpenBLAS backend's exact runtime identity."},
    {"x86_cpuid_identity", cp2_x86_cpuid_identity, METH_NOARGS,
     "Return exact x86 CPUID leaves and decoded machine identity."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef cp2_module = {
    PyModuleDef_HEAD_INIT,
    "cp2_fp_control",
    "Frozen x86_64 floating-point control probe.",
    -1,
    cp2_methods,
    NULL,
    NULL,
    NULL,
    NULL,
};

PyMODINIT_FUNC PyInit_cp2_fp_control(void) {
  PyObject *module = PyModule_Create(&cp2_module);
  if (module == NULL) {
    return NULL;
  }
  if (PyModule_AddIntConstant(module, "EXPECTED_MXCSR", CP2_EXPECTED_MXCSR) < 0 ||
      PyModule_AddIntConstant(module, "EXPECTED_X87_CW", CP2_EXPECTED_X87_CW) < 0 ||
      PyModule_AddIntConstant(module, "CAPSULE_NATIVE", 1) < 0) {
    Py_DECREF(module);
    return NULL;
  }
  return module;
}
