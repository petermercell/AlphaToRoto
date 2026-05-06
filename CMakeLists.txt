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

# CMakeLists.txt - AlphaToRoto Nuke NDK plugin with embedded potrace (Linux)
#
# Usage (pick ONE of):
#   cmake -S . -B build -DNUKE_VERSION=17.0v1               # derives path automatically
#   cmake -S . -B build -DNUKE_INSTALL_PATH=/path/to/nuke   # explicit override
#
# Then:
#   cmake --build build --config Release
#   cmake --install build --prefix ~/.nuke
#
# Potrace 1.16 is already vendored in third_party/potrace/src/.

cmake_minimum_required(VERSION 3.18)
project(AlphaToRoto CXX C)

# ============================================================================
# Linux-only guard. Fail fast on anything else rather than silently mis-building.
# ============================================================================
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
    message(FATAL_ERROR
        "This CMakeLists is Linux-only (got: ${CMAKE_SYSTEM_NAME}).")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ============================================================================
# RPATH: Nuke sets LD_LIBRARY_PATH at runtime so plugins load DDImage via the
# process environment rather than a baked-in RPATH.
# ============================================================================
set(CMAKE_SKIP_BUILD_RPATH   TRUE)
set(CMAKE_SKIP_INSTALL_RPATH TRUE)

# Strip "lib" prefix from output filenames (Nuke plugins are "MyPlugin.so",
# not "libMyPlugin.so").
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
    set(NUKE_INSTALL_PATH "/opt/Nuke${NUKE_VERSION}")
endif()

set(_nuke_include "${NUKE_INSTALL_PATH}/include")
set(_nuke_lib     "${NUKE_INSTALL_PATH}")
set(_nuke_libname "libDDImage.so")

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
    -fPIC
    -fvisibility=hidden)

# ----------------------------------------------------------------------------
# C++ ABI: Nuke 14.x and earlier were built with the old pre-C++11 GCC ABI
# (_GLIBCXX_USE_CXX11_ABI=0). Nuke 15.0+ switched to the new ABI (=1), which
# is also GCC's default on modern toolchains -- so we only force =0 for 14.x.
# Symptom of getting this wrong: undefined symbols like
#   _ZNK2DD5Image2Op12nodeFullPathEc
# where `nm -C libDDImage.so` shows the library side carries an
# [abi:cxx11] tag.
# ----------------------------------------------------------------------------
string(REGEX MATCH "^[0-9]+" _nuke_major "${NUKE_VERSION}")
if(_nuke_major AND _nuke_major LESS 15)
    message(STATUS "Nuke ${_nuke_major}.x detected -- forcing old GCC ABI (_GLIBCXX_USE_CXX11_ABI=0)")
    target_compile_definitions(AlphaToRoto   PRIVATE _GLIBCXX_USE_CXX11_ABI=0)
    target_compile_definitions(potrace_static PRIVATE _GLIBCXX_USE_CXX11_ABI=0)
endif()

set_target_properties(AlphaToRoto PROPERTIES SUFFIX ".so")

# ============================================================================
# Install
# ============================================================================
install(TARGETS AlphaToRoto
    LIBRARY DESTINATION .)

install(FILES
        python/alphaToRoto_py.py
        python/init.py
        python/menu.py
    DESTINATION .)

# ============================================================================
# Summary
# ============================================================================
message(STATUS "")
message(STATUS "========================================")
message(STATUS "AlphaToRoto build configuration (Linux):")
message(STATUS "  Nuke version  : ${NUKE_VERSION}")
message(STATUS "  Nuke root     : ${NUKE_INSTALL_PATH}")
message(STATUS "  Nuke include  : ${_nuke_include}")
message(STATUS "  Nuke lib      : ${_nuke_lib}/${_nuke_libname}")
message(STATUS "  Build type    : ${CMAKE_BUILD_TYPE}")
message(STATUS "  Install prefix: ${CMAKE_INSTALL_PREFIX}")
message(STATUS "========================================")
message(STATUS "")
