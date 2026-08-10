# SPDX-License-Identifier: GPL-3.0-or-later
# Modified in 2026 by Moksh Trehan for SchurVIO-Lite CP2.

cmake_minimum_required(VERSION 3.3)

# Find ROS build system
find_package(catkin QUIET COMPONENTS roscpp rosbag tf std_msgs geometry_msgs sensor_msgs nav_msgs visualization_msgs image_transport cv_bridge ov_core ov_init)

# Describe ROS project
if (catkin_FOUND AND ENABLE_ROS)
    add_definitions(-DROS_AVAILABLE=1)
    catkin_package(
            CATKIN_DEPENDS roscpp rosbag tf std_msgs geometry_msgs sensor_msgs nav_msgs visualization_msgs image_transport cv_bridge ov_core ov_init
            INCLUDE_DIRS src/
            LIBRARIES ov_msckf_lib
    )
else ()
    add_definitions(-DROS_AVAILABLE=0)
    message(WARNING "BUILDING WITHOUT ROS!")
    include(GNUInstallDirs)
    set(CATKIN_PACKAGE_LIB_DESTINATION "${CMAKE_INSTALL_LIBDIR}")
    set(CATKIN_PACKAGE_BIN_DESTINATION "${CMAKE_INSTALL_BINDIR}")
    set(CATKIN_GLOBAL_INCLUDE_DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/open_vins/")
endif ()

# Include our header files
include_directories(
        src
        ${EIGEN3_INCLUDE_DIR}
        ${Boost_INCLUDE_DIRS}
        ${CERES_INCLUDE_DIRS}
        ${catkin_INCLUDE_DIRS}
)

# Set link libraries used by all binaries
list(APPEND thirdparty_libraries
        ${Boost_LIBRARIES}
        ${OpenCV_LIBRARIES}
        ${CERES_LIBRARIES}
        ${catkin_LIBRARIES}
)

# If we are not building with ROS then we need to manually link to its headers
# This isn't that elegant of a way, but this at least allows for building without ROS
# If we had a root cmake we could do this: https://stackoverflow.com/a/11217008/7718197
# But since we don't we need to basically build all the cpp / h files explicitly :(
if (NOT catkin_FOUND OR NOT ENABLE_ROS)

    message(STATUS "MANUALLY LINKING TO OV_CORE LIBRARY....")
    file(GLOB_RECURSE OVCORE_LIBRARY_SOURCES "${CMAKE_SOURCE_DIR}/../ov_core/src/*.cpp")
    list(FILTER OVCORE_LIBRARY_SOURCES EXCLUDE REGEX ".*test_profile\\.cpp$")
    list(FILTER OVCORE_LIBRARY_SOURCES EXCLUDE REGEX ".*test_webcam\\.cpp$")
    list(FILTER OVCORE_LIBRARY_SOURCES EXCLUDE REGEX ".*test_tracking\\.cpp$")
    list(APPEND LIBRARY_SOURCES ${OVCORE_LIBRARY_SOURCES})
    include_directories(${CMAKE_SOURCE_DIR}/../ov_core/src/)
    install(DIRECTORY ${CMAKE_SOURCE_DIR}/../ov_core/src/
            DESTINATION ${CATKIN_GLOBAL_INCLUDE_DESTINATION}
            FILES_MATCHING PATTERN "*.h" PATTERN "*.hpp"
    )

    message(STATUS "MANUALLY LINKING TO OV_INIT LIBRARY....")
    file(GLOB_RECURSE OVINIT_LIBRARY_SOURCES "${CMAKE_SOURCE_DIR}/../ov_init/src/*.cpp")
    list(FILTER OVINIT_LIBRARY_SOURCES EXCLUDE REGEX ".*test_dynamic_init\\.cpp$")
    list(FILTER OVINIT_LIBRARY_SOURCES EXCLUDE REGEX ".*test_dynamic_mle\\.cpp$")
    list(FILTER OVINIT_LIBRARY_SOURCES EXCLUDE REGEX ".*test_simulation\\.cpp$")
    list(FILTER OVINIT_LIBRARY_SOURCES EXCLUDE REGEX ".*Simulator\\.cpp$")
    list(APPEND LIBRARY_SOURCES ${OVINIT_LIBRARY_SOURCES})
    include_directories(${CMAKE_SOURCE_DIR}/../ov_init/src/)
    install(DIRECTORY ${CMAKE_SOURCE_DIR}/../ov_init/src/
            DESTINATION ${CATKIN_GLOBAL_INCLUDE_DESTINATION}
            FILES_MATCHING PATTERN "*.h" PATTERN "*.hpp"
    )

endif ()

##################################################
# Make the shared library
##################################################

list(APPEND LIBRARY_SOURCES
        src/dummy.cpp
        src/sim/Simulator.cpp
        src/state/State.cpp
        src/state/StateHelper.cpp
        src/state/Propagator.cpp
        src/core/VioManager.cpp
        src/core/VioManagerHelper.cpp
        src/update/CP2Canonical.cpp
        src/update/CP2CommitBoundary.cpp
        src/update/CP2CommitOracle.cpp
        src/update/CP2CompositeState.cpp
        src/update/CP2FeatureGate.cpp
        src/update/CP2OfflineReplay.cpp
        src/update/CP2OutputCapability.cpp
        src/update/CP2RuntimeContext.cpp
        src/update/CP2SerialPairing.cpp
        src/update/CP2SerialRuntimeTrace.cpp
        src/update/CP2TimingClock.cpp
        src/update/CP2ShadowMath.cpp
        src/update/CP2StateTraceCodec.cpp
        src/update/CP2TraceCodec.cpp
        src/update/CP2TraceJournal.cpp
        src/update/ConditioningCapture.cpp
        src/update/SchurUpdate.cpp
        src/update/UpdaterHelper.cpp
        src/update/UpdaterMSCKF.cpp
        src/update/UpdaterMSCKFPreview.cpp
        src/update/UpdaterSLAM.cpp
        src/update/UpdaterZeroVelocity.cpp
)

# CP2 evidence and the preview/live-commit oracle have exact binary64
# semantics. Override the repository-wide relaxed signed-zero setting on every
# production translation unit that owns contracted arithmetic, including the
# actual Givens/compression and EKF implementations called by strict wrappers.
set_source_files_properties(
        src/ros1_serial_msckf.cpp
        src/state/StateHelper.cpp
        src/ros/CP2ROS1RuntimeParameters.cpp
        src/update/CP2Canonical.cpp
        src/update/CP2CommitBoundary.cpp
        src/update/CP2CommitOracle.cpp
        src/update/CP2CompositeState.cpp
        src/update/CP2FeatureGate.cpp
        src/update/CP2OfflineReplay.cpp
        src/update/CP2OutputCapability.cpp
        src/update/CP2RecordedAssemble.cpp
        src/update/CP2RuntimeContext.cpp
        src/update/CP2SerialPairing.cpp
        src/update/CP2SerialRuntimeTrace.cpp
        src/update/CP2TimingClock.cpp
        src/update/CP2ShadowMath.cpp
        src/update/CP2StateTraceCodec.cpp
        src/update/CP2TraceCodec.cpp
        src/update/CP2TraceJournal.cpp
        src/update/ConditioningCapture.cpp
        src/update/SchurUpdate.cpp
        src/update/UpdaterHelper.cpp
        src/update/UpdaterMSCKF.cpp
        src/update/UpdaterMSCKFPreview.cpp
        PROPERTIES
        COMPILE_FLAGS "-fno-fast-math -ffp-contract=off -fsigned-zeros")

if (catkin_FOUND AND ENABLE_ROS)
    list(APPEND LIBRARY_SOURCES
            src/ros/CP2ROS1RuntimeParameters.cpp
            src/ros/ROS1Visualizer.cpp
            src/ros/ROSVisualizerHelper.cpp)
endif ()
file(GLOB_RECURSE LIBRARY_HEADERS "src/*.h")
add_library(ov_msckf_lib SHARED ${LIBRARY_SOURCES} ${LIBRARY_HEADERS})
execute_process(
        COMMAND git -C "${CMAKE_CURRENT_SOURCE_DIR}" rev-parse HEAD
        OUTPUT_VARIABLE SCHURVIO_SOURCE_COMMIT_VALUE
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET)
string(LENGTH "${SCHURVIO_SOURCE_COMMIT_VALUE}" SCHURVIO_SOURCE_COMMIT_LENGTH)
if (NOT SCHURVIO_SOURCE_COMMIT_LENGTH EQUAL 40 OR
        NOT SCHURVIO_SOURCE_COMMIT_VALUE MATCHES "^[0-9a-f]+$")
    set(SCHURVIO_SOURCE_COMMIT_VALUE "unknown")
endif ()
target_compile_definitions(ov_msckf_lib PRIVATE
        SCHURVIO_SOURCE_COMMIT="${SCHURVIO_SOURCE_COMMIT_VALUE}")
target_link_libraries(ov_msckf_lib ${thirdparty_libraries})
target_include_directories(ov_msckf_lib PUBLIC src/)
install(TARGETS ov_msckf_lib
        ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)
install(DIRECTORY src/
        DESTINATION ${CATKIN_GLOBAL_INCLUDE_DESTINATION}
        FILES_MATCHING PATTERN "*.h" PATTERN "*.hpp"
)

add_executable(cp2_recorded_assemble src/update/CP2RecordedAssemble.cpp)
target_link_libraries(cp2_recorded_assemble ov_msckf_lib ${thirdparty_libraries})
install(TARGETS cp2_recorded_assemble
        ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)


##################################################
# Make binary files!
##################################################

if (catkin_FOUND AND ENABLE_ROS)

    add_executable(ros1_serial_msckf src/ros1_serial_msckf.cpp)
    target_link_libraries(ros1_serial_msckf ov_msckf_lib ${thirdparty_libraries})
    install(TARGETS ros1_serial_msckf
            ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
            LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
            RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
    )

    add_executable(run_subscribe_msckf src/run_subscribe_msckf.cpp)
    target_link_libraries(run_subscribe_msckf ov_msckf_lib ${thirdparty_libraries})
    install(TARGETS run_subscribe_msckf
            ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
            LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
            RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
    )
    
    install(DIRECTORY launch/
            DESTINATION ${CATKIN_PACKAGE_SHARE_DESTINATION}/launch
    )

endif ()

add_executable(run_simulation src/run_simulation.cpp)
target_link_libraries(run_simulation ov_msckf_lib ${thirdparty_libraries})
install(TARGETS run_simulation
        ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

add_executable(test_sim_meas src/test_sim_meas.cpp)
target_link_libraries(test_sim_meas ov_msckf_lib ${thirdparty_libraries})
install(TARGETS test_sim_meas
        ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

add_executable(test_sim_repeat src/test_sim_repeat.cpp)
target_link_libraries(test_sim_repeat ov_msckf_lib ${thirdparty_libraries})
install(TARGETS test_sim_repeat
        ARCHIVE DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        LIBRARY DESTINATION ${CATKIN_PACKAGE_LIB_DESTINATION}
        RUNTIME DESTINATION ${CATKIN_PACKAGE_BIN_DESTINATION}
)

##################################################
# SchurVIO-Lite CP1 mathematical gates
##################################################
if (CATKIN_ENABLE_TESTING)
    catkin_add_gtest(test_cp1_schur_equivalence
            test/cp1/gtest_main.cpp
            test/cp1/test_schur_equivalence.cpp)
    catkin_add_gtest(test_cp1_rank_rejection
            test/cp1/gtest_main.cpp
            test/cp1/test_rank_rejection.cpp)
    catkin_add_gtest(test_cp1_projection_jacobian
            test/cp1/gtest_main.cpp
            test/cp1/test_projection_jacobian.cpp)
    catkin_add_gtest(test_cp1_prior_and_compression
            test/cp1/gtest_main.cpp
            test/cp1/test_prior_and_compression.cpp)
    catkin_add_gtest(test_cp1_one_pass_regression
            test/cp1/gtest_main.cpp
            test/cp1/test_one_pass_regression.cpp)
    catkin_add_gtest(test_cp1_visual_pass_config
            test/cp1/gtest_main.cpp
            test/cp1/test_visual_pass_config.cpp)
    add_executable(schur_conditioning_benchmark
            test/conditioning/schur_conditioning_benchmark.cpp)
    catkin_add_gtest(test_camera_conditioning_comparators
            test/cp1/gtest_main.cpp
            test/conditioning/CameraConditioningComparators.cpp
            test/conditioning/test_camera_conditioning_comparators.cpp)
    add_executable(camera_conditioning_compare
            test/conditioning/CameraConditioningComparators.cpp
            test/conditioning/camera_conditioning_compare.cpp)
    add_test(NAME test_conditioning_capture_reader
            COMMAND /usr/bin/python3
                    ${CMAKE_CURRENT_SOURCE_DIR}/../experiments/camera_conditioning/test_capture_reader.py)

    set(CP1_TEST_TARGETS
            test_cp1_schur_equivalence
            test_cp1_rank_rejection
            test_cp1_projection_jacobian
            test_cp1_prior_and_compression
            test_cp1_one_pass_regression
            test_cp1_visual_pass_config)
    foreach (CP1_TEST_TARGET ${CP1_TEST_TARGETS})
        if (TARGET ${CP1_TEST_TARGET})
            target_link_libraries(${CP1_TEST_TARGET} ov_msckf_lib ${thirdparty_libraries})
            target_include_directories(${CP1_TEST_TARGET} PRIVATE test/cp1)
            # The repository enables aggressive floating-point flags globally.
            # CP1 numerical gates need strict IEEE behavior and stable operation
            # ordering, so override those flags for these targets only.
            target_compile_options(${CP1_TEST_TARGET} PRIVATE
                    -fno-fast-math
                    -ffp-contract=off
                    -fsigned-zeros)
        endif ()
    endforeach ()
    target_link_libraries(schur_conditioning_benchmark ov_msckf_lib ${thirdparty_libraries})
    target_compile_options(schur_conditioning_benchmark PRIVATE
            -fno-fast-math
            -ffp-contract=off
            -fsigned-zeros)
    if (TARGET test_camera_conditioning_comparators)
        target_link_libraries(test_camera_conditioning_comparators
                ov_msckf_lib ${thirdparty_libraries})
        target_include_directories(test_camera_conditioning_comparators PRIVATE
                test/conditioning)
        target_compile_options(test_camera_conditioning_comparators PRIVATE
                -fno-fast-math
                -ffp-contract=off
                -fsigned-zeros)
    endif ()
    target_link_libraries(camera_conditioning_compare
            ov_msckf_lib ${thirdparty_libraries})
    target_include_directories(camera_conditioning_compare PRIVATE
            test/conditioning)
    target_compile_options(camera_conditioning_compare PRIVATE
            -fno-fast-math
            -ffp-contract=off
            -fsigned-zeros)
endif ()

include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/CP2Tests.cmake)
