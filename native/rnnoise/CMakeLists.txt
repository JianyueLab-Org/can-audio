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
endif()

# v0.2 的发布包里 vec.h / vec_neon.h 在没有走 SSE2/AVX 那条路径时会
# #include "os_support.h" 取 OPUS_CLEAR，但这个头文件从未随 v0.2 一起发布
# （上游主干后来把这些调用点换成了 common.h 自带的 RNN_CLEAR 并删掉了这个
# include，但没有回补到 v0.2）。MSVC 编 x86_64 通常走的是 vec_avx.h，用不上
# 这个符号，但没有 SSE2/AVX 检测宏的目标（比如本机做验证用的 macOS arm64）
# 会掉进这条路径而编译失败。补一个只提供该宏的垫片目录，不改动发布包本身。
target_include_directories(rnnoise PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/compat)
