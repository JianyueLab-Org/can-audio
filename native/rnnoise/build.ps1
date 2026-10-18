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

# Git 自带的 GNU tar 在 PATH 上，遇到 D:\... 这样的路径会当成
# "主机:路径" 的远程写法去解析；显式点系统自带的 tar.exe 才认 Windows 路径。
& "$env:SystemRoot\System32\tar.exe" -xzf $tarball -C $work
if ($LASTEXITCODE) { throw "tar 解包失败" }

# -DRNNOISE_SRC=(Join-Path ...) 在 PowerShell 的参数模式下会被拆成两个词，
# cmake 收到的是空值的 -DRNNOISE_SRC；先算出字符串再拼进一个参数里。
$src = Join-Path $work "rnnoise-0.2"
cmake -S $PSScriptRoot -B (Join-Path $work "build") -A x64 `
      "-DRNNOISE_SRC=$src"
if ($LASTEXITCODE) { throw "cmake 配置失败" }
cmake --build (Join-Path $work "build") --config Release
if ($LASTEXITCODE) { throw "cmake 构建失败" }

Copy-Item (Join-Path $work "build/Release/rnnoise.dll") $Out -Force
Copy-Item (Join-Path $work "rnnoise-0.2/COPYING") (Join-Path $Out "RNNOISE-COPYING") -Force
