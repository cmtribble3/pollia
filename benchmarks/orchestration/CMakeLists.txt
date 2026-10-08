add_executable(pollia-cpp-benchmark cpp.cpp)
target_link_libraries(pollia-cpp-benchmark PRIVATE mitsuba struct-jit)
target_include_directories(pollia-cpp-benchmark PRIVATE
  "${CMAKE_SOURCE_DIR}/ext/nanobind/ext/robin_map/include")
set_target_properties(pollia-cpp-benchmark PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${MI_BINARY_DIR}"
  INTERPROCEDURAL_OPTIMIZATION ON)

add_executable(pollia-cpp-worker worker.cpp)
target_link_libraries(pollia-cpp-worker PRIVATE mitsuba struct-jit)
target_include_directories(pollia-cpp-worker PRIVATE
  "${CMAKE_SOURCE_DIR}/ext/nanobind/ext/robin_map/include")
set_target_properties(pollia-cpp-worker PROPERTIES
  RUNTIME_OUTPUT_DIRECTORY "${MI_BINARY_DIR}"
  INTERPROCEDURAL_OPTIMIZATION ON)
