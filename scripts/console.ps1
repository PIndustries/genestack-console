# Genestack Console — Windows bootstrap.
# Prefers WSL2 (real Linux binary). Otherwise print the Docker Desktop / Git Bash path.
#   irm https://get.genestack.dev/console.ps1 | iex
#   powershell -File console.ps1 --dev   # after saving the file; or use WSL curl | bash --dev
$ErrorActionPreference = "Stop"
$installer = "https://github.com/PIndustries/genestack-console/releases/latest/download/console.sh"
$extra = @($args)

function Quote-Bash([string]$s) {
  "'" + ($s -replace "'", "'\''") + "'"
}

# Extra arguments are forwarded, including: console.ps1 update
$pass = ""
if ($extra.Count -gt 0) {
  $pass = ($extra | ForEach-Object { Quote-Bash $_ }) -join " "
}

$wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
if ($wsl) {
  $distros = @(wsl.exe -l -q 2>$null | Where-Object { $_ -and $_.Trim() -ne "" })
  if ($distros.Count -gt 0) {
    $cmd = "curl -fsSL $installer | bash"
    if ($pass) { $cmd = "curl -fsSL $installer | bash -s -- $pass" }
    Write-Host "WSL2: running the Linux installer"
    & wsl.exe -e bash -lc $cmd
    exit $LASTEXITCODE
  }
}

Write-Host @"
Windows: there is no Win32 Console.

Preferred: WSL2 Ubuntu, then the same one-liner as Linux
  wsl --install -d Ubuntu
  wsl
  curl -fsSL https://get.genestack.dev/console.sh | bash

Laptop lab (UI + seeded demo tenant, no AIO VM): Docker Desktop + Git Bash
  curl -fsSL https://get.genestack.dev/console.sh | bash

--dev (local Ubuntu VM) needs WSL2 with KVM, or macOS/Linux. Not Git Bash.
"@
exit 1
