# TurnSafe T1 P1 value-only certificate/factor core and offline scanner.
# SPDX-License-Identifier: GPL-3.0-or-later

# This library is deliberately separate from ov_msckf_lib. Nothing in the
# live estimator, updater, proposal, commit, or finalization graph links it.
set(TURNSAFE_P1_CORE_CPP_SOURCES
        src/update/TurnSafeCertificate.cpp
        src/update/TurnSafeBearingFactor.cpp
        src/update/TurnSafePairSelector.cpp
        src/update/TurnSafePilotShadow.cpp)
set(TURNSAFE_P1_ALL_CPP_SOURCES
        ${TURNSAFE_P1_CORE_CPP_SOURCES}
        src/update/TurnSafeP1ShadowWorker.cpp)
add_library(turnsafe_p1_core STATIC ${TURNSAFE_P1_CORE_CPP_SOURCES})
target_include_directories(turnsafe_p1_core PUBLIC src/)
target_link_libraries(turnsafe_p1_core ${Boost_LIBRARIES})
target_compile_options(turnsafe_p1_core PRIVATE
        -fno-fast-math
        -ffp-contract=off
        -fsigned-zeros)

# The scanner is an offline archive reader. It is neither installed nor linked
# by a production target.
find_path(TURNSAFE_JSONCPP_INCLUDE_DIR json/json.h
        PATHS /usr/include/jsoncpp)
find_library(TURNSAFE_JSONCPP_LIBRARY NAMES jsoncpp)
find_library(TURNSAFE_ZSTD_LIBRARY NAMES zstd)
find_library(TURNSAFE_CRYPTO_LIBRARY NAMES crypto)
if (TURNSAFE_JSONCPP_INCLUDE_DIR AND TURNSAFE_JSONCPP_LIBRARY AND
        TURNSAFE_ZSTD_LIBRARY AND TURNSAFE_CRYPTO_LIBRARY)
    add_executable(turnsafe_p1_shadow_worker
            src/update/TurnSafeP1ShadowWorker.cpp)
    target_include_directories(turnsafe_p1_shadow_worker PRIVATE
            ${TURNSAFE_JSONCPP_INCLUDE_DIR})
    target_link_libraries(turnsafe_p1_shadow_worker
            turnsafe_p1_core
            ${TURNSAFE_JSONCPP_LIBRARY}
            ${TURNSAFE_ZSTD_LIBRARY}
            ${TURNSAFE_CRYPTO_LIBRARY}
            ${Boost_LIBRARIES})
    target_compile_options(turnsafe_p1_shadow_worker PRIVATE
            -fno-fast-math
            -ffp-contract=off
            -fsigned-zeros)
else ()
    message(FATAL_ERROR
            "TurnSafe P1 scanner requires the existing jsoncpp, zstd, and crypto development libraries")
endif ()

# Configure-time graph/source audit: P1 remains unreachable from every
# production/live target and from the CP2 fault-injection library.
include(${CMAKE_CURRENT_LIST_DIR}/TurnSafeP1Isolation.cmake)

if (CATKIN_ENABLE_TESTING AND COMMAND catkin_add_gtest)
    set(TURNSAFE_P1_TEST_SOURCES
            test_turnsafe_certificate;test/turnsafe/test_turnsafe_certificate.cpp
            test_turnsafe_bearing_factor;test/turnsafe/test_turnsafe_bearing_factor.cpp
            test_turnsafe_pair_selector;test/turnsafe/test_turnsafe_pair_selector.cpp
            test_turnsafe_pilot_shadow;test/turnsafe/test_turnsafe_pilot_shadow.cpp)
    list(LENGTH TURNSAFE_P1_TEST_SOURCES TURNSAFE_P1_TEST_SOURCE_COUNT)
    math(EXPR TURNSAFE_P1_TEST_LAST "${TURNSAFE_P1_TEST_SOURCE_COUNT} - 1")
    foreach (TURNSAFE_P1_TEST_INDEX RANGE 0 ${TURNSAFE_P1_TEST_LAST} 2)
        math(EXPR TURNSAFE_P1_SOURCE_INDEX "${TURNSAFE_P1_TEST_INDEX} + 1")
        list(GET TURNSAFE_P1_TEST_SOURCES ${TURNSAFE_P1_TEST_INDEX}
                TURNSAFE_P1_TEST_TARGET)
        list(GET TURNSAFE_P1_TEST_SOURCES ${TURNSAFE_P1_SOURCE_INDEX}
                TURNSAFE_P1_TEST_SOURCE)
        catkin_add_gtest(${TURNSAFE_P1_TEST_TARGET}
                test/cp1/gtest_main.cpp
                ${TURNSAFE_P1_TEST_SOURCE})
        if (TARGET ${TURNSAFE_P1_TEST_TARGET})
            target_link_libraries(${TURNSAFE_P1_TEST_TARGET}
                    turnsafe_p1_core
                    ${thirdparty_libraries})
            target_compile_options(${TURNSAFE_P1_TEST_TARGET} PRIVATE
                    -fno-fast-math
                    -ffp-contract=off
                    -fsigned-zeros)
        endif ()
    endforeach ()
endif ()
