param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$ConfigPath,
    [string]$TaskName = 'GreenBridge'
)

$ErrorActionPreference = 'Stop'
$runner = Join-Path $PSScriptRoot 'run_green_service.ps1'
foreach ($path in @($PythonExe, $ConfigPath, $runner)) {
    if (-not [System.IO.Path]::IsPathRooted($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw 'Python, configuration and service paths must be absolute existing files.'
    }
    if ($path.Contains('"')) {
        throw 'Paths containing a double quote are not supported.'
    }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw 'A scheduled task with this name already exists; inspect it before making changes.'
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoLogo -NoProfile -NonInteractive -File "{0}" -PythonExe "{1}" -ConfigPath "{2}"' -f $runner, $PythonExe, $ConfigPath
$action = New-ScheduledTaskAction -Execute $powershell -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output "Registered task '$TaskName' and requested a start. Check Task Scheduler and the service log for the actual state."
