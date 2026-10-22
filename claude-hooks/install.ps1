# Install the operator's Claude Code setup for Windows sessions (#1054). A stub: it runs
# claude-hooks/install.sh from master of ~/nomad in WSL, under Git Bash, which does the work.
# Run by the operator, not an agent.
#
#   powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\home\<you>\nomad\claude-hooks\install.ps1 [--dry-run]
#
# Needs Git for Windows (its bash runs the plugin's hooks) and the WSL distro with ~/nomad.
# NOMAD_WSL_DISTRO picks the distro (default Ubuntu-24.04), NOMAD_REF the ref (default master).
$ErrorActionPreference = 'Stop'

$bash = Join-Path $env:ProgramFiles 'Git\bin\bash.exe'
if (-not (Test-Path $bash)) { throw "Git for Windows isn't installed: no $bash" }
$distro = if ($env:NOMAD_WSL_DISTRO) { $env:NOMAD_WSL_DISTRO } else { 'Ubuntu-24.04' }

# No double quotes inside: Windows PowerShell 5.1 mangles them on the way to a native program.
# \~ reaches wsl.exe as ~, the WSL home; git.exe isn't involved, git runs in WSL.
$run = 'export MSYS_NO_PATHCONV=1; d=$1; shift; bash <(wsl.exe -d $d --cd \~ -e git -C nomad show ${NOMAD_REF:-master}:claude-hooks/install.sh) $@'
& $bash -c $run install $distro @args
exit $LASTEXITCODE
