# Keep schemes focused. This runs after Mitsuba and dependency targets exist.
set_property(GLOBAL PROPERTY USE_FOLDERS ON)

function(pollia_group_targets directory)
  get_property(targets DIRECTORY "${directory}" PROPERTY BUILDSYSTEM_TARGETS)
  foreach(target IN LISTS targets)
    set_property(TARGET "${target}" PROPERTY XCODE_GENERATE_SCHEME OFF)
    # Xcode appends a configuration directory to generic output locations.
    # Mitsuba's loader and Python package expect plugins/extensions beside
    # the copied sources, so retain their configured runtime layout explicitly.
    foreach(kind RUNTIME LIBRARY ARCHIVE)
      get_target_property(output "${target}" "${kind}_OUTPUT_DIRECTORY")
      if(output)
        foreach(config DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
          set_property(TARGET "${target}" PROPERTY "${kind}_OUTPUT_DIRECTORY_${config}" "${output}")
        endforeach()
      endif()
    endforeach()
    if(target MATCHES "^pollia_")
      set_property(TARGET "${target}" PROPERTY FOLDER "Pollia Plugins")
    elseif(directory MATCHES "/ext/")
      set_property(TARGET "${target}" PROPERTY FOLDER "Dependencies")
    endif()
  endforeach()
  get_property(children DIRECTORY "${directory}" PROPERTY SUBDIRECTORIES)
  foreach(child IN LISTS children)
    pollia_group_targets("${child}")
  endforeach()
endfunction()
pollia_group_targets("${CMAKE_SOURCE_DIR}")

# Debugged command-line tools have their own Documents privacy identity.
# Stage every launch input beside the products in DerivedData.
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/pollia-scenes" "${CMAKE_BINARY_DIR}/pollia-scripts")
foreach(scene metal-smoke pollia-plugin-smoke)
  configure_file("${CMAKE_SOURCE_DIR}/scenes/${scene}.xml"
    "${CMAKE_BINARY_DIR}/pollia-scenes/${scene}.xml" COPYONLY)
endforeach()
configure_file("${CMAKE_SOURCE_DIR}/scripts/verify-metal.py"
  "${CMAKE_BINARY_DIR}/pollia-scripts/verify-metal.py" COPYONLY)

foreach(target mitsuba-bin pollia_jarecki pollia_pis pollia_mueller)
  set_target_properties(${target} PROPERTIES
    XCODE_GENERATE_SCHEME ON
    XCODE_SCHEME_WORKING_DIRECTORY "${CMAKE_BINARY_DIR}"
    XCODE_SCHEME_EXECUTABLE "${CMAKE_BINARY_DIR}/mitsuba"
    XCODE_SCHEME_LAUNCH_CONFIGURATION "RelWithDebInfo"
    XCODE_SCHEME_ENABLE_GPU_FRAME_CAPTURE_MODE "Metal"
    XCODE_SCHEME_ARGUMENTS "-m;metal_rgb_polarized;${CMAKE_BINARY_DIR}/pollia-scenes/metal-smoke.xml;-o;${CMAKE_BINARY_DIR}/pollia-smoke.exr"
  )
endforeach()
# Let Xcode launch its registered executable product; providing an external
# executable here causes CMake to emit a second, redundant PathRunnable.
set_property(TARGET mitsuba-bin PROPERTY XCODE_SCHEME_EXECUTABLE)
# Exercise RGB transport in both custom plugin schemes.
foreach(target pollia_pis pollia_mueller)
  set_property(TARGET ${target} PROPERTY XCODE_SCHEME_ARGUMENTS
    "-m;metal_rgb_polarized;-D;integrator=${target};${CMAKE_BINARY_DIR}/pollia-scenes/pollia-plugin-smoke.xml;-o;${CMAKE_BINARY_DIR}/${target}-smoke.exr")
endforeach()
set_property(TARGET pollia_jarecki PROPERTY XCODE_SCHEME_ARGUMENTS
  "-m;metal_rgb_polarized;-D;integrator=path;${CMAKE_BINARY_DIR}/pollia-scenes/pollia-plugin-smoke.xml;-o;${CMAKE_BINARY_DIR}/pollia_jarecki-smoke.exr")

# Builds the native executable plus every configured plugin and Python variant.
add_custom_target(Pollia-Backend DEPENDS
  mitsuba-bin ${MI_PLUGIN_TARGETS} ${MI_VARIANT_NAMES}
  mitsuba_ext drjit-python mitsuba-copy-python-src drjit-copy-python-src copy-resources)
set_target_properties(Pollia-Backend PROPERTIES
  FOLDER "Pollia"
  XCODE_GENERATE_SCHEME ON
  XCODE_SCHEME_EXECUTABLE "${CMAKE_BINARY_DIR}/mitsuba"
  XCODE_SCHEME_LAUNCH_CONFIGURATION "RelWithDebInfo"
  XCODE_SCHEME_ENABLE_GPU_FRAME_CAPTURE_MODE "Metal"
  XCODE_SCHEME_ARGUMENTS "-m;metal_rgb_polarized;${CMAKE_BINARY_DIR}/pollia-scenes/metal-smoke.xml;-o;${CMAKE_BINARY_DIR}/pollia-smoke.exr"
  XCODE_SCHEME_WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

add_custom_target(Pollia-Metal-Smoke DEPENDS Pollia-Backend)
set(pollia_launch_python "${Python_EXECUTABLE}")
if(EXISTS "${CMAKE_BINARY_DIR}/pollia-python-env/bin/python")
  set(pollia_launch_python "${CMAKE_BINARY_DIR}/pollia-python-env/bin/python")
endif()
set_target_properties(Pollia-Metal-Smoke PROPERTIES
  FOLDER "Pollia"
  XCODE_GENERATE_SCHEME ON
  XCODE_SCHEME_LAUNCH_CONFIGURATION "RelWithDebInfo"
  XCODE_SCHEME_ENABLE_GPU_FRAME_CAPTURE_MODE "Metal"
  XCODE_SCHEME_EXECUTABLE "${pollia_launch_python}"
  XCODE_SCHEME_ARGUMENTS "${CMAKE_BINARY_DIR}/pollia-scripts/verify-metal.py;--variant;metal_rgb_polarized"
  XCODE_SCHEME_ENVIRONMENT "PYTHONPATH=${CMAKE_BINARY_DIR}/python"
  XCODE_SCHEME_WORKING_DIRECTORY "${CMAKE_BINARY_DIR}")

add_custom_target(Pollia-App
  COMMAND /bin/bash "${CMAKE_SOURCE_DIR}/scripts/build-pollia-app.sh" "${CMAKE_BINARY_DIR}"
  DEPENDS Pollia-Backend
  COMMENT "Build the Pollia SwiftUI app and stage its Python worker"
  USES_TERMINAL)
set_target_properties(Pollia-App PROPERTIES
  FOLDER "Pollia"
  XCODE_GENERATE_SCHEME ON
  XCODE_SCHEME_LAUNCH_CONFIGURATION "RelWithDebInfo"
  XCODE_SCHEME_EXECUTABLE "${CMAKE_BINARY_DIR}/Pollia.app/Contents/MacOS/Pollia"
  XCODE_SCHEME_WORKING_DIRECTORY "${CMAKE_BINARY_DIR}"
  XCODE_SCHEME_ENABLE_GPU_FRAME_CAPTURE_MODE "Metal")
