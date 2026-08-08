#requires -version 5.1
param(
  [ValidateSet('install','uninstall','check')][string]$Action = 'install',
  [string]$Path
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path

function Test-ComfyRoot([string]$Candidate) {
  if ([string]::IsNullOrWhiteSpace($Candidate)) { return $false }
  return (Test-Path -LiteralPath (Join-Path $Candidate 'comfy\ldm\minimax\model.py') -PathType Leaf) -and
         (Test-Path -LiteralPath (Join-Path $Candidate 'custom_nodes') -PathType Container)
}

function Resolve-ComfyRoot([string]$Candidate) {
  if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }
  try { $full = [IO.Path]::GetFullPath($Candidate.Trim('"')) } catch { return $null }
  if (Test-ComfyRoot $full) { return $full }
  $nested = Join-Path $full 'ComfyUI'
  if (Test-ComfyRoot $nested) { return [IO.Path]::GetFullPath($nested) }
  return $null
}

function Find-ComfyRoot([string]$Requested) {
  $resolved = Resolve-ComfyRoot $Requested
  if ($resolved) { return $resolved }

  $parent = Split-Path $repoRoot -Parent
  $candidates = New-Object System.Collections.Generic.List[string]
  foreach ($p in @($repoRoot, $parent, (Join-Path $repoRoot 'ComfyUI'), (Join-Path $parent 'ComfyUI'))) {
    if ($p) { [void]$candidates.Add($p) }
  }
  foreach ($drive in [IO.DriveInfo]::GetDrives()) {
    if (-not $drive.IsReady) { continue }
    $root = $drive.RootDirectory.FullName
    foreach ($rel in @('MiniMaxH3\ComfyUI','ComfyUI','AI\ComfyUI','AI\MiniMaxH3\ComfyUI','ComfyUI-aki-v1.7\ComfyUI','ComfyUI-aki-v3\ComfyUI')) {
      [void]$candidates.Add((Join-Path $root $rel))
    }
  }
  foreach ($candidate in ($candidates | Select-Object -Unique)) {
    $r = Resolve-ComfyRoot $candidate
    if ($r) { return $r }
  }

  $dialog = New-Object Windows.Forms.FolderBrowserDialog
  $dialog.Description = 'Select MiniMaxH3 root OR ComfyUI root (the folder containing comfy and custom_nodes).'
  $dialog.ShowNewFolderButton = $false
  if ($dialog.ShowDialog() -ne [Windows.Forms.DialogResult]::OK) {
    throw 'Folder selection was cancelled.'
  }
  $r = Resolve-ComfyRoot $dialog.SelectedPath
  if (-not $r) { throw "Not a recognized ComfyUI/MiniMaxH3 folder: $($dialog.SelectedPath)" }
  return $r
}

function Find-Python([string]$Comfy) {
  $parent = Split-Path $Comfy -Parent
  $grand = Split-Path $parent -Parent
  $candidates = @(
    (Join-Path $parent 'runtime\venv\Scripts\python.exe'),
    (Join-Path $Comfy 'runtime\venv\Scripts\python.exe'),
    (Join-Path $parent 'python_embeded\python.exe'),
    (Join-Path $parent 'python_embedded\python.exe'),
    (Join-Path $grand 'python_embeded\python.exe'),
    (Join-Path $grand 'python_embedded\python.exe'),
    (Join-Path $Comfy 'python_embeded\python.exe'),
    (Join-Path $Comfy 'python_embedded\python.exe')
  ) | Select-Object -Unique
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $candidate }
  }
  foreach ($name in @('python.exe','python')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
  }
  throw 'Python was not found. Expected MiniMaxH3 runtime\venv, ComfyUI portable python_embeded, or system Python.'
}

try {
  $comfy = Find-ComfyRoot $Path
  $python = Find-Python $comfy
  Write-Host "[OK] ComfyUI: $comfy"
  Write-Host "[OK] Python : $python"
  & $python (Join-Path $repoRoot 'installer.py') $Action --root $comfy
  $rc = $LASTEXITCODE
  if ($null -eq $rc) { $rc = 0 }
  exit $rc
} catch {
  Write-Host ("[ERROR] " + $_.Exception.Message) -ForegroundColor Red
  exit 2
}
