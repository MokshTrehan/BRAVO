# SchurVIO-Lite CP2 mathematical and production-semantics gates.
# SPDX-License-Identifier: GPL-3.0-or-later

if (CATKIN_ENABLE_TESTING)
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
endif ()
