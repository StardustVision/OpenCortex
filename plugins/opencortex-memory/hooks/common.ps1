# Shared helpers for OpenCortex Claude Code hooks (PowerShell).
# PowerShell equivalent of common.sh.

$ErrorActionPreference = "Stop"

# Read stdin (hook input JSON)
$INPUT = if ([Console]::IsInputRedirected) { [Console]::In.ReadToEnd() } else { "" }

# Resolve paths
$SCRIPT_DIR = $PSScriptRoot
$PLUGIN_ROOT = if ($env:CLAUDE_PLUGIN_ROOT) { $env:CLAUDE_PLUGIN_ROOT } else { Split-Path $SCRIPT_DIR -Parent }
$PROJECT_DIR = if ($env:CLAUDE_PROJECT_DIR) { $env:CLAUDE_PROJECT_DIR } else { Get-Location }

$STATE_DIR = Join-Path $PROJECT_DIR ".opencortex" "memory"
$STATE_FILE = Join-Path $STATE_DIR "session_state.json"
$BRIDGE = Join-Path $PLUGIN_ROOT "scripts" "oc_memory.py"

# Config file discovery: project local first, then global default
$CONFIG_FILE = ""
$configCandidates = @(
    (Join-Path $PROJECT_DIR "opencortex.json"),
    (Join-Path $PROJECT_DIR ".opencortex.json"),
    (Join-Path $HOME ".opencortex" "opencortex.json")
)
foreach ($candidate in $configCandidates) {
    if (Test-Path $candidate) {
        $CONFIG_FILE = $candidate
        break
    }
}

# Python resolution: prefer project venv, then system python
$PYTHON_BIN = ""
$venvPython = Join-Path $PROJECT_DIR ".venv" "Scripts" "python.exe"
if (Test-Path $venvPython) {
    $PYTHON_BIN = $venvPython
} else {
    # Also check Unix-style venv path (Git Bash / WSL interop)
    $venvPythonUnix = Join-Path $PROJECT_DIR ".venv" "bin" "python3"
    if (Test-Path $venvPythonUnix) {
        $PYTHON_BIN = $venvPythonUnix
    } elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
        $PYTHON_BIN = "python3"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $PYTHON_BIN = "python"
    }
}

# Ensure PYTHONPATH includes project src so opencortex is importable
$srcPath = Join-Path $PROJECT_DIR "src"
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$srcPath;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $srcPath
}

function _json_val {
    param(
        [string]$Json,
        [string]$Key,
        [string]$Default = ""
    )
    try {
        $obj = $Json | ConvertFrom-Json -ErrorAction Stop
        $parts = $Key -split '\.'
        $val = $obj
        foreach ($part in $parts) {
            if ($null -eq $val) { break }
            $val = $val.$part
        }
        if ($null -eq $val -or $val -eq "") {
            return $Default
        }
        if ($val -is [bool]) {
            return $val.ToString().ToLower()
        }
        return $val.ToString()
    } catch {
        return $Default
    }
}

function _json_encode_str {
    param([string]$Str)
    return ($Str | ConvertTo-Json)
}

$PLUGIN_CONFIG = Join-Path $PLUGIN_ROOT "config.json"

function Get-PluginConfig {
    param(
        [string]$Key,
        [string]$Default = ""
    )
    if (Test-Path $PLUGIN_CONFIG) {
        $content = Get-Content $PLUGIN_CONFIG -Raw
        return (_json_val -Json $content -Key $Key -Default $Default)
    }
    return $Default
}

function Get-PluginMode {
    return (Get-PluginConfig -Key "mode" -Default "local")
}

function Get-HttpUrl {
    $mode = Get-PluginMode
    if ($mode -eq "remote") {
        return (Get-PluginConfig -Key "remote.http_url" -Default "http://127.0.0.1:8921")
    } else {
        $port = Get-PluginConfig -Key "local.http_port" -Default "8921"
        return "http://127.0.0.1:$port"
    }
}

function Get-McpUrl {
    $mode = Get-PluginMode
    if ($mode -eq "remote") {
        return (Get-PluginConfig -Key "remote.mcp_url" -Default "http://127.0.0.1:8920/mcp")
    } else {
        $port = Get-PluginConfig -Key "local.mcp_port" -Default "8920"
        return "http://127.0.0.1:${port}/mcp"
    }
}

function Test-HttpServerReady {
    $url = Get-HttpUrl
    try {
        $null = Invoke-RestMethod -Uri "$url/api/v1/memory/health" -TimeoutSec 2 -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

function Ensure-StateDir {
    if (-not (Test-Path $STATE_DIR)) {
        New-Item -ItemType Directory -Path $STATE_DIR -Force | Out-Null
    }
}

function Invoke-Bridge {
    param([Parameter(ValueFromRemainingArguments)]$Args)

    if (-not $PYTHON_BIN) {
        Write-Output '{"ok": false, "error": "python not found"}'
        return
    }
    if (-not (Test-Path $BRIDGE)) {
        Write-Output '{"ok": false, "error": "bridge script not found"}'
        return
    }

    Ensure-StateDir
    & $PYTHON_BIN $BRIDGE `
        --project-dir $PROJECT_DIR `
        --state-file $STATE_FILE `
        --config $CONFIG_FILE `
        @Args
}
