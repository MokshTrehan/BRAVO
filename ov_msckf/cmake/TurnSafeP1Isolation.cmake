# TurnSafe T1 P1 configure-time production source-graph isolation audit.
# SPDX-License-Identifier: GPL-3.0-or-later

if (NOT TARGET ov_msckf_lib)
    message(FATAL_ERROR
            "TurnSafe P1 isolation audit requires the production ov_msckf_lib target")
endif ()

set(TURNSAFE_P1_PUBLIC_HEADERS
        src/update/TurnSafeCertificate.h
        src/update/TurnSafeBearingFactor.h
        src/update/TurnSafePairSelector.h
        src/update/TurnSafePilotShadow.h)
set(TURNSAFE_P1_FORBIDDEN_SOURCE_TOKENS
        TurnSafeCertificate.h
        TurnSafeBearingFactor.h
        TurnSafePairSelector.h
        TurnSafePilotShadow.h
        TurnSafeCertificate::
        TurnSafeBearingFactor::
        TurnSafePairSelector::
        TurnSafePilotShadow::)

function(turnsafe_p1_absolute_source INPUT_PATH OUTPUT_VARIABLE)
    if (IS_ABSOLUTE "${INPUT_PATH}")
        set(TURNSAFE_P1_ABSOLUTE "${INPUT_PATH}")
    else ()
        set(TURNSAFE_P1_ABSOLUTE
                "${CMAKE_CURRENT_SOURCE_DIR}/${INPUT_PATH}")
    endif ()
    get_filename_component(TURNSAFE_P1_ABSOLUTE
            "${TURNSAFE_P1_ABSOLUTE}" ABSOLUTE)
    set(${OUTPUT_VARIABLE} "${TURNSAFE_P1_ABSOLUTE}" PARENT_SCOPE)
endfunction()

set(TURNSAFE_P1_ISOLATED_CPP_ABSOLUTE)
foreach (TURNSAFE_P1_SOURCE ${TURNSAFE_P1_ALL_CPP_SOURCES})
    turnsafe_p1_absolute_source("${TURNSAFE_P1_SOURCE}"
            TURNSAFE_P1_SOURCE_ABSOLUTE)
    list(APPEND TURNSAFE_P1_ISOLATED_CPP_ABSOLUTE
            "${TURNSAFE_P1_SOURCE_ABSOLUTE}")
endforeach ()
set(TURNSAFE_P1_PUBLIC_HEADER_ABSOLUTE)
foreach (TURNSAFE_P1_HEADER ${TURNSAFE_P1_PUBLIC_HEADERS})
    turnsafe_p1_absolute_source("${TURNSAFE_P1_HEADER}"
            TURNSAFE_P1_HEADER_ABSOLUTE)
    list(APPEND TURNSAFE_P1_PUBLIC_HEADER_ABSOLUTE
            "${TURNSAFE_P1_HEADER_ABSOLUTE}")
endforeach ()

# The CP2 fault target is conditional on CATKIN_ENABLE_TESTING. Audit its
# definition even in release-only configurations where that target is absent.
set(TURNSAFE_P1_CP2_GRAPH_FILE
        "${CMAKE_CURRENT_SOURCE_DIR}/cmake/CP2Tests.cmake")
file(READ "${TURNSAFE_P1_CP2_GRAPH_FILE}" TURNSAFE_P1_CP2_GRAPH_CONTENT)
foreach (TURNSAFE_P1_SOURCE ${TURNSAFE_P1_ALL_CPP_SOURCES})
    get_filename_component(TURNSAFE_P1_SOURCE_NAME "${TURNSAFE_P1_SOURCE}" NAME)
    string(FIND "${TURNSAFE_P1_CP2_GRAPH_CONTENT}"
            "${TURNSAFE_P1_SOURCE_NAME}" TURNSAFE_P1_CP2_SOURCE_POSITION)
    if (NOT TURNSAFE_P1_CP2_SOURCE_POSITION EQUAL -1)
        message(FATAL_ERROR
                "TurnSafe P1 isolation violation: CP2 target graph names ${TURNSAFE_P1_SOURCE_NAME}")
    endif ()
endforeach ()
string(FIND "${TURNSAFE_P1_CP2_GRAPH_CONTENT}" "turnsafe_p1_core"
        TURNSAFE_P1_CP2_LINK_POSITION)
if (NOT TURNSAFE_P1_CP2_LINK_POSITION EQUAL -1)
    message(FATAL_ERROR
            "TurnSafe P1 isolation violation: CP2 target graph links turnsafe_p1_core")
endif ()

function(turnsafe_p1_assert_target_isolated TARGET_NAME)
    if (NOT TARGET ${TARGET_NAME})
        return()
    endif ()

    get_target_property(TURNSAFE_P1_TARGET_LINKS ${TARGET_NAME} LINK_LIBRARIES)
    if (TURNSAFE_P1_TARGET_LINKS)
        string(FIND "${TURNSAFE_P1_TARGET_LINKS}" "turnsafe_p1_core"
                TURNSAFE_P1_LINK_POSITION)
        if (NOT TURNSAFE_P1_LINK_POSITION EQUAL -1)
            message(FATAL_ERROR
                    "TurnSafe P1 isolation violation: ${TARGET_NAME} links turnsafe_p1_core")
        endif ()
    endif ()

    get_target_property(TURNSAFE_P1_TARGET_SOURCES ${TARGET_NAME} SOURCES)
    if (NOT TURNSAFE_P1_TARGET_SOURCES)
        return()
    endif ()
    foreach (TURNSAFE_P1_TARGET_SOURCE ${TURNSAFE_P1_TARGET_SOURCES})
        if (TURNSAFE_P1_TARGET_SOURCE MATCHES "^\\$<")
            message(FATAL_ERROR
                    "TurnSafe P1 isolation cannot audit generator-expression source in ${TARGET_NAME}: ${TURNSAFE_P1_TARGET_SOURCE}")
        endif ()
        turnsafe_p1_absolute_source("${TURNSAFE_P1_TARGET_SOURCE}"
                TURNSAFE_P1_TARGET_SOURCE_ABSOLUTE)

        list(FIND TURNSAFE_P1_ISOLATED_CPP_ABSOLUTE
                "${TURNSAFE_P1_TARGET_SOURCE_ABSOLUTE}"
                TURNSAFE_P1_CPP_SOURCE_POSITION)
        if (NOT TURNSAFE_P1_CPP_SOURCE_POSITION EQUAL -1)
            message(FATAL_ERROR
                    "TurnSafe P1 isolation violation: ${TARGET_NAME} owns ${TURNSAFE_P1_TARGET_SOURCE}")
        endif ()

        list(FIND TURNSAFE_P1_PUBLIC_HEADER_ABSOLUTE
                "${TURNSAFE_P1_TARGET_SOURCE_ABSOLUTE}"
                TURNSAFE_P1_PUBLIC_HEADER_POSITION)
        if (NOT TURNSAFE_P1_PUBLIC_HEADER_POSITION EQUAL -1)
            continue()
        endif ()
        if (NOT EXISTS "${TURNSAFE_P1_TARGET_SOURCE_ABSOLUTE}")
            continue()
        endif ()
        get_filename_component(TURNSAFE_P1_TARGET_SOURCE_EXTENSION
                "${TURNSAFE_P1_TARGET_SOURCE_ABSOLUTE}" EXT)
        if (NOT TURNSAFE_P1_TARGET_SOURCE_EXTENSION MATCHES
                "^\\.(c|cc|cpp|cxx|h|hh|hpp|hxx)$")
            continue()
        endif ()
        file(READ "${TURNSAFE_P1_TARGET_SOURCE_ABSOLUTE}"
                TURNSAFE_P1_TARGET_SOURCE_CONTENT)
        foreach (TURNSAFE_P1_FORBIDDEN_TOKEN
                ${TURNSAFE_P1_FORBIDDEN_SOURCE_TOKENS})
            string(FIND "${TURNSAFE_P1_TARGET_SOURCE_CONTENT}"
                    "${TURNSAFE_P1_FORBIDDEN_TOKEN}"
                    TURNSAFE_P1_FORBIDDEN_TOKEN_POSITION)
            if (NOT TURNSAFE_P1_FORBIDDEN_TOKEN_POSITION EQUAL -1)
                message(FATAL_ERROR
                        "TurnSafe P1 isolation violation: ${TARGET_NAME} source ${TURNSAFE_P1_TARGET_SOURCE} references ${TURNSAFE_P1_FORBIDDEN_TOKEN}")
            endif ()
        endforeach ()
    endforeach ()
endfunction()

# Libraries bind the production and CP2-fault translation-unit graphs. The
# executable checks additionally guard every estimator-facing entry point.
set(TURNSAFE_P1_PRODUCTION_TARGETS
        ov_msckf_lib
        ov_msckf_cp2_fault_lib
        ros1_serial_msckf
        run_subscribe_msckf
        run_simulation
        cp2_recorded_assemble
        test_sim_meas
        test_sim_repeat)
foreach (TURNSAFE_P1_PRODUCTION_TARGET ${TURNSAFE_P1_PRODUCTION_TARGETS})
    turnsafe_p1_assert_target_isolated(${TURNSAFE_P1_PRODUCTION_TARGET})
endforeach ()

message(STATUS
        "TurnSafe P1 isolation audit passed: no production/live source or CP2 fault target reaches P1")
