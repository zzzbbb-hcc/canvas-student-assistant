[CmdletBinding()]
param(
    [switch]$InstallBuildTools,
    [switch]$SkipTests,
    [switch]$SkipAudit
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "StudentAssistant.exe must be built on Windows."
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Virtual environment not found. Create .venv and install requirements.txt first."
}

Push-Location $ProjectRoot
try {
    if ($InstallBuildTools) {
        & $Python -m pip install -r requirements-build.txt
        if ($LASTEXITCODE -ne 0) { throw "Could not install build tools." }
    }

    & $Python -c "import PyInstaller"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is not installed. Re-run with -InstallBuildTools."
    }

    if (-not $SkipTests) {
        & $Python -m pytest
        if ($LASTEXITCODE -ne 0) { throw "Tests failed; package was not built." }
    }

    if (-not $SkipAudit) {
        & $Python scripts\security_audit.py
        if ($LASTEXITCODE -ne 0) { throw "Pre-build security audit failed." }
    }

    & $Python -m PyInstaller --noconfirm --clean StudentAssistant.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

    # Ship only the fixed public translation model, never an entire user-data
    # directory. It lives next to the executable rather than in private cache.
    $ModelSource = Join-Path $ProjectRoot "models\en_zh"
    $ModelTarget = Join-Path $ProjectRoot "dist\StudentAssistant\models\en_zh"
    $ModelFiles = @("metadata.json", "sentencepiece.model", "README.md", "model\config.json", "model\shared_vocabulary.json", "model\model.bin")
    foreach ($ModelFile in $ModelFiles) {
        $ModelInput = Join-Path $ModelSource $ModelFile
        if (-not (Test-Path -LiteralPath $ModelInput -PathType Leaf)) {
            throw "Offline translation model is missing. Run scripts\setup_translation_model.py first."
        }
        $ModelOutput = Join-Path $ModelTarget $ModelFile
        New-Item -ItemType Directory -Path (Split-Path -Parent $ModelOutput) -Force | Out-Null
        Copy-Item -LiteralPath $ModelInput -Destination $ModelOutput -Force
    }

    if (-not $SkipAudit) {
        & $Python scripts\security_audit.py --include-build
        if ($LASTEXITCODE -ne 0) { throw "Post-build security audit failed." }
    }

    $Executable = Join-Path $ProjectRoot "dist\StudentAssistant\StudentAssistant.exe"
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Build completed without the expected executable: $Executable"
    }

    $Hash = Get-FileHash -LiteralPath $Executable -Algorithm SHA256
    Write-Host "Build complete: $Executable"
    Write-Host "SHA256: $($Hash.Hash)"
}
finally {
    Pop-Location
}
