# xiph RNNoise v0.2 → rnnoise.dll（MSVC）。源文件表与 pyrnnoise 的 CMakeLists 一致；
# 发布包里已带 src/rnnoise_data.c，不需要构建时下载模型。
cmake_minimum_required(VERSION 3.14)
project(rnnoise LANGUAGES C)

set(RNNOISE_SRC "" CACHE PATH "Path to the unpacked rnnoise-0.2 directory")
if(NOT RNNOISE_SRC)
  message(FATAL_ERROR "set -DRNNOISE_SRC=<unpacked rnnoise-0.2>")
endif()

add_library(rnnoise SHARED
  ${RNNOISE_SRC}/src/celt_lpc.c
  ${RNNOISE_SRC}/src/denoise.c
  ${RNNOISE_SRC}/src/kiss_fft.c
  ${RNNOISE_SRC}/src/parse_lpcnet_weights.c
  ${RNNOISE_SRC}/src/pitch.c
  ${RNNOISE_SRC}/src/rnn.c
  ${RNNOISE_SRC}/src/rnnoise_data.c
  ${RNNOISE_SRC}/src/rnnoise_tables.c
  ${RNNOISE_SRC}/src/nnet.c
  ${RNNOISE_SRC}/src/nnet_default.c
)
target_include_directories(rnnoise PRIVATE ${RNNOISE_SRC}/include ${RNNOISE_SRC}/src)
if(MSVC)
  target_compile_definitions(rnnoise PRIVATE DLL_EXPORT RNNOISE_BUILD)
  # vec.h 在没有 __SSE2__/__AVX__ 时走通用路径，那条路径用了 C99 的
  # restrict 关键字；MSVC 的 C 编译器不认它，直接语法报错。MSVC 从不预定义
  # __SSE2__/__AVX__（不论目标是不是 x86_64），所以这条通用路径在 MSVC 下
  # 总会被编到，不是只有没有 SIMD 检测宏的平台才会走进去。把 restrict 定义
  # 成 MSVC 自带的 __restrict，对每个 VS 工具集都管用。
  target_compile_definitions(rnnoise PRIVATE restrict=__restrict)
endif()

# v0.2 的发布包里 vec.h / vec_neon.h 在没有走 SSE2/AVX 那条路径时会
# #include "os_support.h" 取 OPUS_CLEAR，但这个头文件从未随 v0.2 一起发布
# （上游主干后来把这些调用点换成了 common.h 自带的 RNN_CLEAR 并删掉了这个
# include，但没有回补到 v0.2）。这条通用路径在 MSVC 下总会被编到（见上），
# 在没有 SSE2/AVX 检测宏的目标（比如本机做验证用的 macOS arm64）上也会走到。
# 补一个只提供该宏的垫片目录，不改动发布包本身。
target_include_directories(rnnoise PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/compat)
