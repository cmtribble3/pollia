# ----------------------------------------------------------
#  Mitsuba library
# ----------------------------------------------------------

# Some parts of Mitsuba use the TSL hash map class
include_directories(../ext/nanobind/ext/robin_map/include)

add_subdirectory(core)
add_subdirectory(render)

add_library(mitsuba SHARED)
if (CMAKE_GENERATOR STREQUAL "Xcode")
  # Xcode skips the link step for a shared target made only of object libraries.
  set(MI_XCODE_ANCHOR "${CMAKE_CURRENT_BINARY_DIR}/mitsuba_xcode_anchor.cpp")
  file(WRITE "${MI_XCODE_ANCHOR}" "extern \"C\" void mitsuba_xcode_anchor() {}\n")
  target_sources(mitsuba PRIVATE "${MI_XCODE_ANCHOR}")
endif()
target_link_libraries(mitsuba PUBLIC mitsuba-core)
target_link_libraries(mitsuba PUBLIC mitsuba-render)
set_target_properties(mitsuba PROPERTIES INTERPROCEDURAL_OPTIMIZATION ON)

if (CMAKE_CXX_COMPILER_ID MATCHES "^(GNU)$")
  # Treat undefined symbols as errors when linking
  target_link_libraries(mitsuba PRIVATE -Wl,--no-undefined)
  # Silence an incorrect warning message in GCC LTO builds
  target_link_options(mitsuba PRIVATE -Walloc-size-larger-than=18446744073709551615)
endif()

# ----------------------------------------------------------
#  Mitsuba executable
# ----------------------------------------------------------

add_subdirectory(mitsuba)

# ----------------------------------------------------------
#  Plugins
# ----------------------------------------------------------

add_subdirectory(bsdfs)
add_subdirectory(emitters)
add_subdirectory(films)
add_subdirectory(integrators)
add_subdirectory(media)
add_subdirectory(phase)
add_subdirectory(rfilters)
add_subdirectory(samplers)
add_subdirectory(sensors)
add_subdirectory(shapes)
add_subdirectory(spectra)
add_subdirectory(textures)
add_subdirectory(volumes)
set(MI_PLUGIN_TARGETS "${MI_PLUGIN_TARGETS}" PARENT_SCOPE)

# ----------------------------------------------------------
#  Python bindings and extensions
# ----------------------------------------------------------

if (MI_ENABLE_PYTHON)
  add_subdirectory(python)
endif()
