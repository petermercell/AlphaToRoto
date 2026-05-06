# SPDX-License-Identifier: GPL-3.0-only
#
# AlphaToRoto -- Trace alpha-channel mattes into Roto shapes in Nuke
# Copyright (C) 2026 Peter Mercell
#
# This file is part of AlphaToRoto.
#
# AlphaToRoto is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, version 3 of the License.
#
# AlphaToRoto is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with AlphaToRoto. If not, see <https://www.gnu.org/licenses/>.
#
# AlphaToRoto links statically against potrace (Copyright (C) 2001-2019
# Peter Selinger), which is licensed under "GPL version 2, or (at your
# option) any later version". AlphaToRoto exercises the "or later" option
# to combine potrace under GPL-3.0. See the LICENCE file at the repository
# root and third_party/potrace/COPYING for full details.

# CMakeLists.txt - AlphaToRoto Nuke NDK plugin with embedded potrace (macOS)
#
# Usage (pick ONE of):
#   cmake -S . -B build -DNUKE_VERSION=17.0v1               # derives path automatically
#   cmake -S . -B build -DNUKE_INSTALL_PATH=/path/to/nuke   # explicit override
#
# Then:
#   cmake --build build --config Release
#
# This produces build/AlphaToRoto.dylib. Copy it (and the contents of python/)
# into ~/.nuke or your studio's NUKE_PATH location manually.
#
# Potrace 1.16 is already vendored in third_party/potrace/src/.

cmake_minimum_required(VERSION 3.18)
project(AlphaToRoto CXX C)

# ============================================================================
# macOS-only guard. Fail fast on anything else rather than silently mis-building.
# ============================================================================
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Darwin")
    message(FATAL_ERROR
        "This CMakeLists is macOS-only (got: ${CMAKE_SYSTEM_NAME}).")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ============================================================================
# RPATH / install-name: Nuke sets DYLD_LIBRARY_PATH at runtime so plugins load
# DDImage via the process environment rather than a baked-in RPATH.
# ============================================================================
set(CMAKE_SKIP_BUILD_RPATH   TRUE)
set(CMAKE_SKIP_INSTALL_RPATH TRUE)
set(CMAKE_MACOSX_RPATH       FALSE)

# Strip "lib" prefix from output filenames (Nuke plugins are "MyPlugin.dylib",
# not "libMyPlugin.dylib").
set(CMAKE_SHARED_LIBRARY_PREFIX "")

# ============================================================================
# Nuke location
# ============================================================================
set(NUKE_VERSION      "" CACHE STRING "Nuke version, e.g. '17.0v1'. Derives install path automatically.")
set(NUKE_INSTALL_PATH "" CACHE PATH   "Explicit Nuke install root. Overrides NUKE_VERSION.")

# If NUKE_INSTALL_PATH not explicitly set, derive from NUKE_VERSION.
if(NOT NUKE_INSTALL_PATH)
    if(NOT NUKE_VERSION)
        message(FATAL_ERROR
            "Please pass either -DNUKE_VERSION=17.0v1 "
            "or -DNUKE_INSTALL_PATH=<path to Nuke install>")
    endif()
    set(NUKE_INSTALL_PATH "/Applications/Nuke${NUKE_VERSION}")
endif()

# If NUKE_VERSION wasn't supplied but we have a path, try to extract the
# version from the last path component (e.g. "/Applications/Nuke17.0v1") so
# we can find the versioned .app folder inside it.
if(NOT NUKE_VERSION)
    get_filename_component(_leaf "${NUKE_INSTALL_PATH}" NAME)
    string(REGEX REPLACE "^[Nn]uke" "" NUKE_VERSION "${_leaf}")
endif()

# DDImage on macOS lives inside the versioned .app bundle:
#   /Applications/Nuke17.0v1/Nuke17.0v1.app/Contents/MacOS
set(_nuke_macos   "${NUKE_INSTALL_PATH}/Nuke${NUKE_VERSION}.app/Contents/MacOS")
set(_nuke_include "${_nuke_macos}/include")
set(_nuke_lib     "${_nuke_macos}")
set(_nuke_libname "libDDImage.dylib")

if(NOT EXISTS "${_nuke_include}")
    message(FATAL_ERROR
        "Nuke include dir not found: ${_nuke_include}\n"
        "Check that NUKE_VERSION='${NUKE_VERSION}' matches your install, "
        "or set NUKE_INSTALL_PATH directly.")
endif()
if(NOT EXISTS "${_nuke_lib}/${_nuke_libname}")
    message(FATAL_ERROR
        "DDImage library not found: ${_nuke_lib}/${_nuke_libname}")
endif()

# ============================================================================
# macOS architecture default -- match host unless caller overrides.
# ============================================================================
if(NOT CMAKE_OSX_ARCHITECTURES)
    execute_process(
        COMMAND uname -m
        OUTPUT_VARIABLE _host_arch
        OUTPUT_STRIP_TRAILING_WHITESPACE)
    set(CMAKE_OSX_ARCHITECTURES "${_host_arch}" CACHE STRING
        "Build architecture (auto-detected from host)")
endif()

# ============================================================================
# potrace (static, embedded)
# ============================================================================
set(POTRACE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/third_party/potrace"
    CACHE PATH "Path to potrace source tree (containing src/)")

set(_potrace_src "${POTRACE_ROOT}/src")

set(_potrace_files
    "${_potrace_src}/potracelib.c"
    "${_potrace_src}/curve.c"
    "${_potrace_src}/decompose.c"
    "${_potrace_src}/trace.c"
)

foreach(_f IN LISTS _potrace_files)
    if(NOT EXISTS "${_f}")
        message(FATAL_ERROR
            "potrace source file missing: ${_f}\n"
            "See BUILD.md for how to vendor potrace 1.16.")
    endif()
endforeach()

add_library(potrace_static STATIC ${_potrace_files})
target_include_directories(potrace_static PUBLIC "${_potrace_src}")

# Potrace without autoconf: we supply the two things its library code actually
# looks at. Do NOT define HAVE_CONFIG_H at all (not even =0) -- the sources use
# #ifdef, so any definition triggers `#include <config.h>`.
target_compile_definitions(potrace_static PRIVATE
    HAVE_INTTYPES_H=1
    VERSION="1.16"
)

target_compile_options(potrace_static PRIVATE
    -Wall -Wno-unused-function -Wno-unused-variable)

# ============================================================================
# AlphaToRoto plugin
# ============================================================================
add_library(AlphaToRoto SHARED src/AlphaToRoto.cpp)

target_include_directories(AlphaToRoto PRIVATE
    "${_nuke_include}"
    "${_potrace_src}"
    src
)

target_link_libraries(AlphaToRoto PRIVATE
    potrace_static
    "${_nuke_lib}/${_nuke_libname}"
)

target_compile_options(AlphaToRoto PRIVATE
    -fvisibility=hidden)

# ----------------------------------------------------------------------------
# C++ ABI note: macOS uses Clang + libc++, so the GCC-only
# _GLIBCXX_USE_CXX11_ABI flag does not apply here. Nuke's macOS DDImage and
# any plugin built with the system Clang/Apple-Clang share the same libc++
# ABI by default, so no extra flag is needed.
# ----------------------------------------------------------------------------

set_target_properties(AlphaToRoto PROPERTIES
    SUFFIX ".dylib"
    INSTALL_NAME_DIR "@rpath"
    BUILD_WITH_INSTALL_NAME_DIR TRUE)

# ============================================================================
# Install: intentionally omitted. Installation is manual -- after building,
# copy build/AlphaToRoto.dylib and the contents of python/ to your Nuke plugin
# path (typically ~/.nuke).
# ============================================================================

# ============================================================================
# Summary
# ============================================================================
message(STATUS "")
message(STATUS "========================================")
message(STATUS "AlphaToRoto build configuration (macOS):")
message(STATUS "  Nuke version  : ${NUKE_VERSION}")
message(STATUS "  Nuke root     : ${NUKE_INSTALL_PATH}")
message(STATUS "  Nuke include  : ${_nuke_include}")
message(STATUS "  Nuke lib      : ${_nuke_lib}/${_nuke_libname}")
message(STATUS "  Architecture  : ${CMAKE_OSX_ARCHITECTURES}")
message(STATUS "  Build type    : ${CMAKE_BUILD_TYPE}")
message(STATUS "========================================")
message(STATUS "")