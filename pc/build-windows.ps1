<#
Build the engine fork (pc/engine) on Windows with the installed Visual Studio,
or, with -Writer, the Stage 2 world writer (pc/wldwriter) against it.

The fork's projects ask for toolset v140 and Windows SDK 10.0.14393; its CI
builds them on a VS 2019 image. Here they are built with whatever MSVC is
installed (v143 on VS 2022) and the newest Windows SDK, passed as global
msbuild properties, so the pinned submodule stays untouched. The fixes in
pc/patches are applied to its checkout before an engine build.

    pc\build-windows.ps1                                 # SamTSE, Release x64, whole solution
    pc\build-windows.ps1 -Target Engine                  # one project (and what it depends on)
    pc\build-windows.ps1 -Target "Ecc;Engine;EntitiesMP"
    pc\build-windows.ps1 -Game SamTFE -Config Debug
    pc\build-windows.ps1 -Writer                         # pc/wldwriter, into SamTSE/Bin
    pc\build-windows.ps1 -Entities                       # pc/entities, NE's creatures, into SamTSE/Bin

Needs: VS 2022 (Build Tools is enough) with "Desktop development with C++"
and ATL/MFC, and the Vulkan SDK (VULKAN_SDK). See pc/README.md.
#>
param(
    [string]$Game = "SamTSE",
    [string]$Config = "Release",
    [string]$Platform = "x64",
    [string]$Target = "",
    [string]$Toolset = "v143",
    [string]$Sdk = "10.0",
    [switch]$Writer,
    [switch]$Entities
)
$ErrorActionPreference = "Stop"

$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path $vswhere)) { throw "vswhere not found: install Visual Studio 2022 or its Build Tools" }
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw "no Visual Studio with the C++ tools (Microsoft.VisualStudio.Component.VC.Tools.x86.x64)" }
$msbuild = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\MSBuild.exe" | Select-Object -First 1
if (-not $msbuild) { throw "MSBuild not found under $vs" }
if (-not (Test-Path "$vs\VC\Tools\MSVC\*\atlmfc\include\afx.h")) {
    throw "ATL/MFC is missing (Microsoft.VisualStudio.Component.VC.ATLMFC); every engine project uses MFC"
}

# A freshly installed SDK sets VULKAN_SDK machine-wide; this shell may predate it.
if (-not $env:VULKAN_SDK) { $env:VULKAN_SDK = [Environment]::GetEnvironmentVariable("VULKAN_SDK", "Machine") }
if (-not $env:VULKAN_SDK -or -not (Test-Path "$env:VULKAN_SDK\Include\vulkan\vulkan.h")) {
    throw "Vulkan SDK not found (VULKAN_SDK='$env:VULKAN_SDK')"
}

if ($Writer) {
    $sln = Join-Path $PSScriptRoot "wldwriter\WldWriter.vcxproj"
    if (-not (Test-Path (Join-Path $PSScriptRoot "engine\SamTSE\Sources\Engine\Release\Engine.lib"))) {
        throw "build the engine first: pc\build-windows.ps1 -Target Engine"
    }
} elseif ($Entities) {
    $sln = Join-Path $PSScriptRoot "entities\EntitiesNE.vcxproj"
    if (-not (Test-Path (Join-Path $PSScriptRoot "engine\SamTSE\Sources\EntitiesMP\Release\EntitiesMP.lib"))) {
        throw "build the engine and EntitiesMP first: pc\build-windows.ps1 -Target `"Engine;EntitiesMP`""
    }
} else {
    $sln = Join-Path $PSScriptRoot "engine\$Game\Sources\$Game.sln"
    if (-not (Test-Path $sln)) { throw "solution not found: $sln (is the pc/engine submodule checked out?)" }
    # Fixes to the fork live as patches in pc/patches, applied to the checkout
    # before building; the submodule stays pinned to upstream. See pc/README.md.
    $engine = Join-Path $PSScriptRoot "engine"
    foreach ($patch in Get-ChildItem (Join-Path $PSScriptRoot "patches\*.patch")) {
        # through cmd: git's messages on stderr would stop this script
        cmd /c "git -C `"$engine`" apply --reverse --check `"$($patch.FullName)`" 2>nul"
        if ($LASTEXITCODE -eq 0) { continue }                     # already applied
        cmd /c "git -C `"$engine`" apply `"$($patch.FullName)`" 2>&1"
        if ($LASTEXITCODE -ne 0) { throw "patch does not apply to pc/engine: $($patch.Name)" }
        Write-Host "Patched : $($patch.Name)"
    }
}

$props = @("/m", "/nologo", "/v:minimal",
           "/p:Configuration=$Config", "/p:Platform=$Platform",
           "/p:PlatformToolset=$Toolset", "/p:WindowsTargetPlatformVersion=$Sdk")
if ($Target) { $props += "/t:$Target" }

Write-Host "MSBuild : $msbuild"
Write-Host "Vulkan  : $env:VULKAN_SDK"
Write-Host "Building: $sln [$Config|$Platform] toolset $Toolset, SDK $Sdk $(if ($Target) { "targets $Target" })"
& $msbuild $sln @props
exit $LASTEXITCODE
