
param(
    [string]$RepoUrl = "https://github.com/lellag/leak_reolocation.git",
    [string]$RepoDir = (Join-Path $env:USERPROFILE "Downloads\leak_reolocation"),
    [string]$Branch = "main",
    [string]$CommitMessage = "Update LeakRelocation working files",
    [switch]$NoPush,
    [switch]$IncludeViewerArtifacts
)

$ErrorActionPreference = "Stop"

function Write-Step($Message) {
    Write-Host "" 
    Write-Host "=== $Message ===" -ForegroundColor Cyan
}

function Ensure-GitAvailable {
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($null -eq $git) {
        throw "Git was not found in PATH. Install Git for Windows, then rerun this script."
    }
    return $git.Source
}

function Ensure-Directory($Path) {
    if (!(Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Copy-IfExists($Source, $Destination) {
    if (Test-Path $Source) {
        Ensure-Directory (Split-Path -Parent $Destination)
        Copy-Item -Path $Source -Destination $Destination -Force
        Write-Host "Copied: $Source -> $Destination" -ForegroundColor Green
        return $true
    }
    Write-Host "Skipped missing: $Source" -ForegroundColor DarkYellow
    return $false
}

function Copy-MatchingFiles($SourceDir, $Pattern, $DestinationDir) {
    if (!(Test-Path $SourceDir)) {
        Write-Host "Skipped missing folder: $SourceDir" -ForegroundColor DarkYellow
        return
    }
    Ensure-Directory $DestinationDir
    Get-ChildItem -Path $SourceDir -Filter $Pattern -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object {
            Copy-Item -Path $_.FullName -Destination (Join-Path $DestinationDir $_.Name) -Force
            Write-Host "Copied: $($_.FullName) -> $DestinationDir" -ForegroundColor Green
        }
}

$Git = Ensure-GitAvailable
$WorkRoot = Join-Path $env:USERPROFILE "Downloads\LeakRelocation-GeoPandas"
$Downloads = Join-Path $env:USERPROFILE "Downloads"
$NetworkProject = "\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation"
$NetworkScript = Join-Path $NetworkProject "Arcpy Code\leak reolcation - geopandas.py"
$OutputRoot = Join-Path $WorkRoot "production_moved_leak_outputs"
$LocalViewerWithPipes = Join-Path $OutputRoot "local_leaflet_view_with_pipes"
$LocalViewerBasic = Join-Path $OutputRoot "local_leaflet_view"
$Log = Join-Path $Downloads ("Sync-LeakRelocation-ToGit_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")

Start-Transcript -Path $Log -Force | Out-Null
try {
    Write-Step "Git repo setup"
    Write-Host "Repository URL: $RepoUrl"
    Write-Host "Repository folder: $RepoDir"
    Write-Host "Branch: $Branch"
    Write-Host "Git: $Git"

    if (!(Test-Path $RepoDir)) {
        Ensure-Directory (Split-Path -Parent $RepoDir)
        git clone $RepoUrl $RepoDir
    }

    Set-Location $RepoDir

    if (!(Test-Path (Join-Path $RepoDir ".git"))) {
        throw "Folder exists but is not a Git repository: $RepoDir"
    }

    git remote set-url origin $RepoUrl
    git fetch origin --prune

    $branchExists = $false
    $localBranches = git branch --list $Branch
    if ($localBranches) { $branchExists = $true }

    if ($branchExists) {
        git checkout $Branch
    } else {
        git checkout -B $Branch
    }

    try {
        git pull --ff-only origin $Branch
    } catch {
        Write-Host "git pull --ff-only did not complete. Continuing with local branch state." -ForegroundColor DarkYellow
    }

    Write-Step "Create repo folders"
    $RepoSrc = Join-Path $RepoDir "src"
    $RepoScripts = Join-Path $RepoDir "scripts"
    $RepoPowershell = Join-Path $RepoDir "powershell"
    $RepoDocs = Join-Path $RepoDir "docs"
    $RepoViewer = Join-Path $RepoDir "viewer"
    Ensure-Directory $RepoSrc
    Ensure-Directory $RepoScripts
    Ensure-Directory $RepoPowershell
    Ensure-Directory $RepoDocs
    Ensure-Directory $RepoViewer

    Write-Step "Write .gitignore"
    $GitIgnore = @'
# Local caches and generated data
layer_cache/
assettype_cache_enrichment/
assettype_sample_debug/
assettype_only_small_test/
assettype_domain_diagnostics/
material_domain_diagnostics_v2/
production_moved_leak_outputs/**/*.csv
production_moved_leak_outputs/**/*.geojson
production_moved_leak_outputs/**/*.gpkg
production_moved_leak_outputs/**/*.log
production_moved_leak_outputs/local_leaflet_view/relocated_*.geojson
production_moved_leak_outputs/local_leaflet_view_with_pipes/*.geojson
*.pkl.gz
*.gpkg
*.gdb/
*.fgdb/
*.sqlite
*.db
*.log
*.bak
*.tmp
*.zip

# Secrets and auth artifacts
*token*
*Token*
*.secret
*.secrets
.env
.env.*
credentials.json

# Python
__pycache__/
*.pyc
.venv/
venv/

# OS/editor
.DS_Store
Thumbs.db
.vscode/
.idea/
'@
    Set-Content -Path (Join-Path $RepoDir ".gitignore") -Value $GitIgnore -Encoding UTF8

    Write-Step "Copy primary source files"
    Copy-IfExists $NetworkScript (Join-Path $RepoSrc "leak_relocation_geopandas.py") | Out-Null
    Copy-IfExists (Join-Path $WorkRoot "leaflet_bbox_server.py") (Join-Path $RepoSrc "leaflet_bbox_server.py") | Out-Null

    Write-Step "Copy working Python scripts from local project"
    $PythonKeepPatterns = @(
        "TEST_*.py",
        "RUN_*.py",
        "check_*.py",
        "audit_*.py",
        "build_local_*.py",
        "small_test_*.py",
        "sample_*.py",
        "enrich_*.py",
        "patch_*.py",
        "preflight_*.py",
        "inspect_*.py"
    )
    foreach ($pattern in $PythonKeepPatterns) {
        Copy-MatchingFiles $WorkRoot $pattern $RepoScripts
    }

    Write-Step "Copy PowerShell helper scripts"
    $PowerShellPatterns = @(
        "*LeakRelocation*.ps1",
        "*Assettype*.ps1",
        "*ASSETTYPE*.ps1",
        "Create-LocalLeakRelocationViewer*.ps1",
        "Audit-ProductionRelocation*.ps1",
        "Proceed-LeakRelocation*.ps1",
        "Rewrite-And-Run-ServiceAssettype*.ps1",
        "Run-ServiceAssettype*.ps1",
        "Run-StableServiceAssettype*.ps1"
    )
    foreach ($pattern in $PowerShellPatterns) {
        Copy-MatchingFiles $Downloads $pattern $RepoPowershell
    }

    if (Test-Path $LocalViewerWithPipes) {
        Copy-IfExists (Join-Path $LocalViewerWithPipes "index.html") (Join-Path $RepoViewer "index_with_pipes.html") | Out-Null
        Copy-IfExists (Join-Path $LocalViewerWithPipes "Start-LocalRelocationViewer.ps1") (Join-Path $RepoViewer "Start-LocalRelocationViewer-WithPipes.ps1") | Out-Null
    }
    if (Test-Path $LocalViewerBasic) {
        Copy-IfExists (Join-Path $LocalViewerBasic "index.html") (Join-Path $RepoViewer "index_basic.html") | Out-Null
        Copy-IfExists (Join-Path $LocalViewerBasic "Start-LocalRelocationViewer.ps1") (Join-Path $RepoViewer "Start-LocalRelocationViewer-Basic.ps1") | Out-Null
    }

    if ($IncludeViewerArtifacts) {
        Write-Step "Copy optional viewer artifacts"
        $ArtifactDir = Join-Path $RepoDir "viewer_artifacts"
        Ensure-Directory $ArtifactDir
        if (Test-Path $LocalViewerWithPipes) {
            Copy-Item -Path (Join-Path $LocalViewerWithPipes "*") -Destination $ArtifactDir -Recurse -Force
            Write-Host "Copied viewer artifacts to: $ArtifactDir" -ForegroundColor Green
        }
    }

    Write-Step "Write project state docs"
    $State = @"
# LeakRelocation project state

Generated by `Sync-LeakRelocation-ToGit.ps1` on $(Get-Date -Format "yyyy-MM-dd HH:mm:ss").

## Current workflow summary

- Authoritative pipe material classification is `ASSETGROUP + ASSETTYPE`, decoded from DNV subtype domains.
- DNV field `material` is treated as Grade/characteristic data, not the material classification for relocation assessment.
- Local corrected pipe caches preserve the original grade field as `GradeMaterial`.
- Local corrected pipe caches use decoded `ASSETTYPE` as `material` for compatibility with the existing relocation logic.
- Production GeoPackage output path:
  `\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation\HistoricLeakRelocation.gpkg`
- Local output folder:
  `C:\Users\lellag\Downloads\LeakRelocation-GeoPandas\production_moved_leak_outputs`

## Important generated outputs not committed by default

Large/generated data files are intentionally ignored by `.gitignore`, including:

- layer cache pickle files
- GeoPackages
- GeoJSON viewer exports
- CSV relocation outputs
- logs
- token or credential artifacts

Use the local project folder or network project folder for generated data products.
"@
    Set-Content -Path (Join-Path $RepoDocs "PROJECT_STATE.md") -Value $State -Encoding UTF8

    $ReadmePath = Join-Path $RepoDir "README.md"
    if (!(Test-Path $ReadmePath)) {
        $Readme = @"
# leak_reolocation

Leak relocation workflow for DNV / GeoPandas production processing.

## Key rule

Use decoded `ASSETGROUP + ASSETTYPE` for pipe material classification. Do not use the DNV `material` Grade field as the material class for relocation assessment.

## Local workflow

Current working files are synchronized from:

- `C:\Users\lellag\Downloads\LeakRelocation-GeoPandas`
- `\\ngusnasnwh001\gasne\GasNE Shared\Shared\ENG\Complex Team\GIS AutoPrint\Distribution Leak Relocation`

Run PowerShell helper scripts from the `powershell/` folder.
"@
        Set-Content -Path $ReadmePath -Value $Readme -Encoding UTF8
    }

    Write-Step "Git status before commit"
    git status --short

    $changes = git status --porcelain
    if (-not $changes) {
        Write-Host "No Git changes detected. Nothing to commit." -ForegroundColor Yellow
    } else {
        git add .
        git commit -m $CommitMessage
        if (-not $NoPush) {
            git push -u origin $Branch
        } else {
            Write-Host "NoPush specified. Commit created locally but not pushed." -ForegroundColor Yellow
        }
    }

    Write-Step "Final Git status"
    git status --short
    $Head = git rev-parse --short HEAD
    $Remote = git remote get-url origin

    $Summary = @(
        "=== LeakRelocation Git Sync Complete ===",
        "RepoUrl: $RepoUrl",
        "RepoDir: $RepoDir",
        "Branch: $Branch",
        "HEAD: $Head",
        "Remote: $Remote",
        "Pushed: $(-not $NoPush)",
        "Log: $Log"
    ) -join "`r`n"

    $Summary | Set-Clipboard
    Write-Host ""
    Write-Host $Summary -ForegroundColor Green
}
finally {
    Stop-Transcript | Out-Null
}
