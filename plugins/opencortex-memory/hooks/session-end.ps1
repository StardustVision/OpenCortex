# SessionEnd hook: store session summary, kill local servers.
#
# 1. Store session summary via bridge (best-effort)
# 2. Kill HTTP + MCP server PIDs (local mode only)
# 3. Mark session state inactive

. "$PSScriptRoot\common.ps1"

if (-not (Test-Path $STATE_FILE)) {
    exit 0
}

# ---------------------------------------------------------------------------
# Store session summary via bridge (best-effort)
# ---------------------------------------------------------------------------
if ($CONFIG_FILE -and (Test-Path $CONFIG_FILE)) {
    try {
        Invoke-Bridge session-end | Out-Null
    } catch {}
}

# ---------------------------------------------------------------------------
# Kill local servers if we started them
# ---------------------------------------------------------------------------
$stateContent = '{}'
try { $stateContent = Get-Content $STATE_FILE -Raw } catch {}

$MODE = _json_val -Json $stateContent -Key "mode" -Default "local"

if ($MODE -eq "local") {
    $HTTP_PID = _json_val -Json $stateContent -Key "http_pid" -Default "0"
    $MCP_PID  = _json_val -Json $stateContent -Key "mcp_pid"  -Default "0"

    if ([int]$MCP_PID -gt 0) {
        Stop-Process -Id ([int]$MCP_PID) -Force -ErrorAction SilentlyContinue
    }

    if ([int]$HTTP_PID -gt 0) {
        Stop-Process -Id ([int]$HTTP_PID) -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
$INGESTED = _json_val -Json $stateContent -Key "ingested_turns" -Default "0"
$STATUS = "[opencortex-memory] session ended - turns=$INGESTED"
$jsonStatus = _json_encode_str $STATUS
Write-Output "{`"systemMessage`": $jsonStatus}"
