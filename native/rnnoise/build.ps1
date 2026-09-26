# 下载 xiph RNNoise v0.2 发布包、校验、构建 rnnoise.dll，输出到 -Out 目录。
param([string]$Out = "$PSScriptRoot/out")
$ErrorActionPreference = "Stop"

$url = "https://github.com/xiph/rnnoise/releases/download/v0.2/rnnoise-0.2.tar.gz"
$sha = "90fce4b00b9ff24c08dbfe31b82ffd43bae383d85c5535676d28b0a2b11c0d37"
$work = Join-Path $PSScriptRoot "work"
New-Item -ItemType Directory -Force -Path $work, $Out | Out-Null

$tarball = Join-Path $work "rnnoise-0.2.tar.gz"
Invoke-WebRequest -Uri $url -OutFile $tarball
$actual = (Get-FileHash -Algorithm SHA256 $tarball).Hash.ToLower()
if ($actual -ne $sha) { throw "rnnoise-0.2.tar.gz SHA-256 mismatch: $actual" }

tar -xzf $tarball -C $work
cmake -S $PSScriptRoot -B (Join-Path $work "build") -A x64 `
      -DRNNOISE_SRC=(Join-Path $work "rnnoise-0.2")
cmake --build (Join-Path $work "build") --config Release
Copy-Item (Join-Path $work "build/Release/rnnoise.dll") $Out -Force
Copy-Item (Join-Path $work "rnnoise-0.2/COPYING") (Join-Path $Out "RNNOISE-COPYING") -Force
