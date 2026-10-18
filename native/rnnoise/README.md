# rnnoise

`denoise.py` 通过 ctypes 加载的 RNNoise 原生库。

- 来源：xiph RNNoise v0.2 发布包，BSD-3，SHA-256 固定在 `build.ps1`。
- Windows：`pwsh native/rnnoise/build.ps1 -Out controller`（需要 CMake 和 MSVC）。CI 在 `release.yml` 的 `rnnoise` job 里执行同一脚本。
- macOS 开发：`brew install rnnoise`。
- 产物 `rnnoise.dll` 不进仓库。
- `compat/os_support.h` 补上 v0.2 发布包里 `vec.h` 会 include、但从未随包发布的 `OPUS_CLEAR` 宏。
