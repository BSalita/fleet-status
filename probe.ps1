# One status snapshot of this Windows machine as JSON on stdout (fleet-status).
# server.py pipes it to "powershell -NoProfile -Command -" (locally, or over SSH) after putting in
# __LOGSCAN_B64__ (logscan.py), __RUNTIME__ and __WSLC_SESSION__. Reads only; changes nothing.
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$out = [ordered]@{}
$out.host = $env:COMPUTERNAME
$out.os = 'windows'
$out.user = $env:USERNAME
$out.time = (Get-Date).ToString('o')

$cs = Get-CimInstance Win32_ComputerSystem
$os = Get-CimInstance Win32_OperatingSystem
$out.logical_cpus = [int]$cs.NumberOfLogicalProcessors
$out.mem_total_kb = [int64]$os.TotalVisibleMemorySize
$out.mem_free_kb = [int64]$os.FreePhysicalMemory
$out.boot = $os.LastBootUpTime.ToString('o')

$c = Get-Counter '\Processor Information(_Total)\% Processor Time' -SampleInterval 1 -MaxSamples 1
if ($c) { $out.cpu_pct = [math]::Round($c.CounterSamples[0].CookedValue, 1) }

$nv = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nv) {
    $g = & nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $g) { $out.gpu = @($g) }
}

$out.disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
    [ordered]@{ id = $_.DeviceID; size = [int64]$_.Size; free = [int64]$_.FreeSpace } })

# Is the owner signed in on the desktop? wslc build/run need it.
$q = (quser 2>$null) -join "`n"
$out.sessions = $q

# Windows processes: CPU time for the server's delta; command lines only for script hosts.
$scripty = '^(python|pythonw|py|powershell|pwsh|cmd|node|cargo|rustc|cl|link|msbuild|robocopy|java)\.exe$'
$out.procs = @(Get-CimInstance Win32_Process | ForEach-Object {
    $t = [int64]$_.UserModeTime + [int64]$_.KernelModeTime
    $cmd = $null
    if ($_.Name -match $scripty) { $cmd = $_.CommandLine; if ($cmd -and $cmd.Length -gt 400) { $cmd = $cmd.Substring(0, 400) } }
    $created = $null
    if ($_.CreationDate) { $created = $_.CreationDate.ToString('o') }
    [ordered]@{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = $_.Name; t = $t; ws = [int64]$_.WorkingSetSize; cmd = $cmd; created = $created }
})

# Containers: wslc (WSL containers), docker or podman, as the config says for this machine.
$runtime = '__RUNTIME__'
$exe = $null; $pre = @()
if ($runtime -eq 'wslc') {
    $exe = 'C:\Program Files\WSL\wslc.exe'
    $sess = '__WSLC_SESSION__'
    if (-not $sess) { $sess = "wslc-cli-$($env:USERNAME.ToLower())" }
    $pre = @('--session', $sess)
    if (-not (Test-Path $exe)) { $exe = $null }
} elseif ($runtime -eq 'docker' -or $runtime -eq 'podman') {
    if (Get-Command $runtime -ErrorAction SilentlyContinue) { $exe = $runtime }
}
if ($exe) {
    if ($runtime -eq 'wslc') {
        $list = & $exe @pre list --all --format json 2>$null
        $stats = & $exe @pre stats --format json 2>$null
    } else {
        $list = & $exe ps -a --no-trunc --format '{{json .}}' 2>$null
        $stats = & $exe stats --no-stream --format '{{json .}}' 2>$null
    }
    $out.containers = @($list | Where-Object { $_ -like '{*' })
    $out.stats = @($stats | Where-Object { $_ -like '{*' })
    $ps = [ordered]@{}
    $logs = [ordered]@{}
    $logscan = '__LOGSCAN_B64__'
    foreach ($line in $out.containers) {
        $o = $line | ConvertFrom-Json
        if ("$($o.State)" -eq 'running') {
            $n = @($o.Names)[0]
            $r = & $exe @pre exec $n ps -eo 'sid=,pid=,ppid=,etimes=,cputimes=,rss=,args=' --cols 400 2>$null
            $ps[$n] = ($r -join "`n")
            # Log files the container's processes write (logscan.py, put here by server.py).
            $lg = & $exe @pre exec $n python3 -c "import base64;exec(base64.b64decode('$logscan'))" 2>$null
            $logs[$n] = ($lg | Where-Object { $_ -like '{*' } | Select-Object -Last 1)
        }
    }
    $out.ps = $ps
    $out.logs = $logs
}
$out.done = (Get-Date).ToString('o')
$out | ConvertTo-Json -Depth 6 -Compress

