/* xiph RNNoise v0.2 发布包遗漏的头文件，不是发布包本身的一部分。
 *
 * vec.h / vec_neon.h 在没有走 SSE2/AVX 那条路径时会 #include "os_support.h"
 * 取 OPUS_CLEAR，但这个文件从未随 v0.2 tarball 一起发布——上游主干后来把
 * 这些调用点换成了 common.h 自带的 RNN_CLEAR 并删掉了这个 include，但没有
 * 回补到 v0.2。MSVC 编 x86_64 通常会走 vec_avx.h（不需要这个符号），但
 * ARM（比如本机做验证用的 macOS）以及任何没有 SSE2/AVX 检测宏的目标会
 * 掉进这条路径。这里只补一个宏，不改动发布包本身的任何源文件。
 */
#ifndef OS_SUPPORT_H
#define OS_SUPPORT_H

#include "common.h"

#ifndef OPUS_CLEAR
#define OPUS_CLEAR(dst, n) RNN_CLEAR((dst), (n))
#endif

#endif
