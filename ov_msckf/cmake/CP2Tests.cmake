# SchurVIO-Lite CP2 mathematical and production-semantics gates.
# SPDX-License-Identifier: GPL-3.0-or-later

if (CATKIN_ENABLE_TESTING)
    # A fully separate, non-installed test library carries closed fault
    # controls. The production ov_msckf_lib remains macro-clean, and no test
    # executable links both definitions.
    add_library(ov_msckf_cp2_fault_lib STATIC
            ${LIBRARY_SOURCES}
            ${LIBRARY_HEADERS})
    target_compile_definitions(ov_msckf_cp2_fault_lib PRIVATE
            OV_MSCKF_CP2_TESTING=1)
    target_link_libraries(ov_msckf_cp2_fault_lib ${thirdparty_libraries})
    target_include_directories(ov_msckf_cp2_fault_lib PUBLIC src/)

    catkin_add_gtest(test_cp2_production_schur_reducer
            test/cp2/gtest_main.cpp
            test/cp2/test_production_schur_reducer.cpp)

    if (TARGET test_cp2_production_schur_reducer)
        target_link_libraries(test_cp2_production_schur_reducer
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_production_schur_reducer PRIVATE
                test/cp2)
        # The repository globally enables non-IEEE floating-point flags. CP2
        # boundary and parity gates require strict operation semantics.
        target_compile_options(test_cp2_production_schur_reducer PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_fej_golden
            test/cp2/gtest_main.cpp
            test/cp2/test_fej_golden.cpp)

    if (TARGET test_cp2_fej_golden)
        target_link_libraries(test_cp2_fej_golden
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_fej_golden PRIVATE
                test/cp2)
        target_compile_options(test_cp2_fej_golden PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_state_update_semantics
            test/cp2/gtest_main.cpp
            test/cp2/test_state_update_semantics.cpp)

    if (TARGET test_cp2_state_update_semantics)
        target_link_libraries(test_cp2_state_update_semantics
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_state_update_semantics PRIVATE
                test/cp2)
        target_compile_options(test_cp2_state_update_semantics PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_configuration_contract
            test/cp2/gtest_main.cpp
            test/cp2/test_configuration_contract.cpp)

    if (TARGET test_cp2_configuration_contract)
        target_link_libraries(test_cp2_configuration_contract
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_configuration_contract PRIVATE
                test/cp2)
        target_compile_options(test_cp2_configuration_contract PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_updater_msckf_end_to_end
            test/cp2/gtest_main.cpp
            test/cp2/test_updater_msckf_end_to_end.cpp)

    if (TARGET test_cp2_updater_msckf_end_to_end)
        target_link_libraries(test_cp2_updater_msckf_end_to_end
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_updater_msckf_end_to_end PRIVATE
                test/cp2)
        target_compile_options(test_cp2_updater_msckf_end_to_end PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_updater_msckf_fault_injection
            test/cp2/gtest_main.cpp
            test/cp2/test_updater_msckf_end_to_end.cpp)

    if (TARGET test_cp2_updater_msckf_fault_injection)
        target_link_libraries(test_cp2_updater_msckf_fault_injection
                ov_msckf_cp2_fault_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_updater_msckf_fault_injection PRIVATE
                test/cp2)
        target_compile_options(test_cp2_updater_msckf_fault_injection PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
        target_compile_definitions(test_cp2_updater_msckf_fault_injection PRIVATE
                OV_MSCKF_CP2_TESTING=1)
    endif ()

    # Keep the composite-state gate in its own executable: it interposes the C
    # heap and all ordinary C++ new forms to prove that the prepared phase-3
    # fill/handoff stays allocation-free under injected allocation failure.
    catkin_add_gtest(test_cp2_composite_state
            test/cp2/gtest_main.cpp
            test/cp2/test_cp2_composite_state.cpp)

    if (TARGET test_cp2_composite_state)
        target_link_libraries(test_cp2_composite_state
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_composite_state PRIVATE
                test/cp2)
        target_compile_options(test_cp2_composite_state PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_commit_oracle
            test/cp2/gtest_main.cpp
            test/cp2/test_cp2_commit_oracle.cpp)

    if (TARGET test_cp2_commit_oracle)
        target_link_libraries(test_cp2_commit_oracle
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_commit_oracle PRIVATE
                test/cp2)
        target_compile_options(test_cp2_commit_oracle PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    catkin_add_gtest(test_cp2_commit_boundary
            test/cp2/gtest_main.cpp
            test/cp2/test_cp2_commit_boundary.cpp)

    if (TARGET test_cp2_commit_boundary)
        target_link_libraries(test_cp2_commit_boundary
                ov_msckf_lib
                ${thirdparty_libraries})
        target_include_directories(test_cp2_commit_boundary PRIVATE
                test/cp2)
        target_compile_options(test_cp2_commit_boundary PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()

    set(CP2_VALUE_ONLY_TEST_SOURCES
            test_cp2_canonical;test/cp2/test_cp2_canonical.cpp
            test_cp2_feature_gate;test/cp2/test_cp2_feature_gate.cpp
            test_cp2_updater_msckf_preview_snapshot;test/cp2/test_updater_msckf_preview_snapshot.cpp
            test_cp2_shadow_math;test/cp2/test_cp2_shadow_math.cpp
            test_cp2_trace_codec;test/cp2/test_cp2_trace_codec.cpp)
    list(LENGTH CP2_VALUE_ONLY_TEST_SOURCES CP2_VALUE_ONLY_TEST_SOURCE_COUNT)
    math(EXPR CP2_VALUE_ONLY_TEST_LAST "${CP2_VALUE_ONLY_TEST_SOURCE_COUNT} - 1")
    foreach (CP2_VALUE_ONLY_TEST_INDEX RANGE 0 ${CP2_VALUE_ONLY_TEST_LAST} 2)
        math(EXPR CP2_VALUE_ONLY_SOURCE_INDEX "${CP2_VALUE_ONLY_TEST_INDEX} + 1")
        list(GET CP2_VALUE_ONLY_TEST_SOURCES ${CP2_VALUE_ONLY_TEST_INDEX} CP2_VALUE_ONLY_TARGET)
        list(GET CP2_VALUE_ONLY_TEST_SOURCES ${CP2_VALUE_ONLY_SOURCE_INDEX} CP2_VALUE_ONLY_SOURCE)
        catkin_add_gtest(${CP2_VALUE_ONLY_TARGET}
                test/cp2/gtest_main.cpp
                ${CP2_VALUE_ONLY_SOURCE})
        if (TARGET ${CP2_VALUE_ONLY_TARGET})
            target_link_libraries(${CP2_VALUE_ONLY_TARGET}
                    ov_msckf_lib
                    ${thirdparty_libraries})
            target_include_directories(${CP2_VALUE_ONLY_TARGET} PRIVATE
                    test/cp2)
            target_compile_options(${CP2_VALUE_ONLY_TARGET} PRIVATE
                    -fno-fast-math
                    -ffp-contract=off
                    -fsigned-zeros)
        endif ()
    endforeach ()
endif ()
