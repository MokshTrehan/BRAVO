// SPDX-License-Identifier: GPL-3.0-or-later
/* Frozen x86_64 floating-point control probe for CP2-D capsules.
 *
 * Exception-status bits are sticky observations, not arithmetic controls.
 * The probe therefore masks them before comparison and clears them after a
 * successful check.  DAZ and FTZ remain control bits and are both rejected.
 */

#include <stdint.h>

#if !defined(__x86_64__)
#error "cp2_fp_control.c is frozen for x86_64"
#endif

#define CP2_EXPECTED_MXCSR ((uint32_t)0x00001f80u)
#define CP2_MXCSR_CONTROL_MASK ((uint32_t)0x0000ffc0u)
#define CP2_MXCSR_STATUS_MASK ((uint32_t)0x0000003fu)
/* Gradual-underflow arithmetic may set denormal, underflow, and inexact. */
#define CP2_MXCSR_PERMITTED_STATUS ((uint32_t)0x00000032u)
#define CP2_EXPECTED_X87_CW ((uint16_t)0x027fu)
#define CP2_X87_STATUS_MASK ((uint16_t)0x003fu)
#define CP2_X87_PERMITTED_STATUS ((uint16_t)0x0032u)

static uint32_t cp2_read_mxcsr(void) {
  uint32_t value;
  __asm__ volatile("stmxcsr %0" : "=m"(value));
  return value;
}

static uint16_t cp2_read_x87_cw(void) {
  uint16_t value;
  __asm__ volatile("fnstcw %0" : "=m"(value));
  return value;
}

static uint16_t cp2_read_x87_sw(void) {
  uint16_t value;
  __asm__ volatile("fnstsw %0" : "=am"(value));
  return value;
}

int cp2_fp_establish(uint32_t *observed_mxcsr, uint16_t *observed_x87_cw,
                     uint16_t *observed_x87_sw) {
  const uint32_t mxcsr = CP2_EXPECTED_MXCSR;
  const uint16_t x87_cw = CP2_EXPECTED_X87_CW;
  __asm__ volatile("ldmxcsr %0" : : "m"(mxcsr));
  __asm__ volatile("fnclex\n\tfldcw %0" : : "m"(x87_cw));
  const uint32_t observed_mxcsr_value = cp2_read_mxcsr();
  const uint16_t observed_x87_cw_value = cp2_read_x87_cw();
  const uint16_t observed_x87_sw_value = cp2_read_x87_sw();
  if (observed_mxcsr != 0) {
    *observed_mxcsr = observed_mxcsr_value;
  }
  if (observed_x87_cw != 0) {
    *observed_x87_cw = observed_x87_cw_value;
  }
  if (observed_x87_sw != 0) {
    *observed_x87_sw = observed_x87_sw_value;
  }
  return observed_mxcsr_value == CP2_EXPECTED_MXCSR &&
                 observed_x87_cw_value == CP2_EXPECTED_X87_CW &&
                 (observed_x87_sw_value & CP2_X87_STATUS_MASK) == 0u
             ? 0
             : 1;
}

int cp2_fp_verify(uint32_t *observed_mxcsr, uint16_t *observed_x87_cw,
                  uint16_t *observed_x87_sw) {
  const uint32_t raw_mxcsr = cp2_read_mxcsr();
  const uint16_t raw_x87_cw = cp2_read_x87_cw();
  const uint16_t raw_x87_sw = cp2_read_x87_sw();
  const uint32_t normalized_mxcsr = raw_mxcsr & CP2_MXCSR_CONTROL_MASK;
  if (observed_mxcsr != 0) {
    *observed_mxcsr = raw_mxcsr;
  }
  if (observed_x87_cw != 0) {
    *observed_x87_cw = raw_x87_cw;
  }
  if (observed_x87_sw != 0) {
    *observed_x87_sw = raw_x87_sw;
  }
  if (normalized_mxcsr != CP2_EXPECTED_MXCSR ||
      raw_x87_cw != CP2_EXPECTED_X87_CW ||
      (raw_mxcsr & CP2_MXCSR_STATUS_MASK &
       ~CP2_MXCSR_PERMITTED_STATUS) != 0u ||
      (raw_x87_sw & CP2_X87_STATUS_MASK &
       ~CP2_X87_PERMITTED_STATUS) != 0u) {
    return 1;
  }
  /* Clear permitted sticky observations only after rejecting fatal status. */
  __asm__ volatile("ldmxcsr %0" : : "m"(normalized_mxcsr));
  __asm__ volatile("fnclex");
  return 0;
}
