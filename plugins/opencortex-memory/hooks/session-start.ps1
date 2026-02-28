# SessionStart hook: start servers (local) and initialize memory session.
#
# Local mode:  start HTTP server + MCP server in background, save PIDs.
# Remote mode: verify remote HTTP server is reachable.

. "$PSScriptRoot\common.ps1"

Ensure-StateDir

# ---------------------------------------------------------------------------
# Validate config
# ---------------------------------------------------------------------------
if (-not $CONFIG_FILE -or -not (Test-Path $CONFIG_FILE)) {
    $msg = '[opencortex-memory] WARNING: config not found. Create opencortex.json or $HOME/.opencortex/opencortex.json'
    $jsonMsg = _json_encode_str $msg
    Write-Output "{`"systemMessage`": $jsonMsg}"
    exit 0
}

$MODE = Get-PluginMode
$HTTP_URL = Get-HttpUrl

# ---------------------------------------------------------------------------
# Local mode: start HTTP server + MCP server
# ---------------------------------------------------------------------------
if ($MODE -eq "local") {
    $HTTP_PORT = Get-PluginConfig -Key "local.http_port" -Default "8921"
    $MCP_PORT  = Get-PluginConfig -Key "local.mcp_port"  -Default "8920"

    $HTTP_PID = 0
    $MCP_PID  = 0

    # Check if HTTP server is already running
    if (-not (Test-HttpServerReady)) {
        # Start HTTP server
        $httpLog = Join-Path $STATE_DIR "http.log"
        $httpArgs = "-m opencortex.http --config `"$CONFIG_FILE`" --port $HTTP_PORT --log-level WARNING"
        $httpProc = Start-Process -FilePath $PYTHON_BIN -ArgumentList $httpArgs `
            -NoNewWindow -RedirectStandardOutput $httpLog -RedirectStandardError (Join-Path $STATE_DIR "http_err.log") `
            -PassThru

        $HTTP_PID = $httpProc.Id

        # Wait for HTTP server to be ready (max 10s)
        foreach ($_ in 1..10) {
            if (Test-HttpServerReady) { break }
            Start-Sleep -Seconds 1
        }

        if (-not (Test-HttpServerReady)) {
            $msg = "[opencortex-memory] WARNING: HTTP server failed to start on port $HTTP_PORT"
            $jsonMsg = _json_encode_str $msg
            Write-Output "{`"systemMessage`": $jsonMsg}"
            exit 0
        }
    }

    # Check if MCP server is already running
    $mcpAlive = $false
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:$MCP_PORT/mcp" -TimeoutSec 2 -ErrorAction Stop
        $mcpAlive = $true
    } catch {}

    if (-not $mcpAlive) {
        # Start MCP server in remote mode (forwards to HTTP server)
        $mcpLog = Join-Path $STATE_DIR "mcp.log"
        $mcpArgs = "-m opencortex.mcp_server --config `"$CONFIG_FILE`" --transport streamable-http --port $MCP_PORT --mode remote --log-level WARNING"
        $mcpProc = Start-Process -FilePath $PYTHON_BIN -ArgumentList $mcpArgs `
            -NoNewWindow -RedirectStandardOutput $mcpLog -RedirectStandardError (Join-Path $STATE_DIR "mcp_err.log") `
            -PassThru

        $MCP_PID = $mcpProc.Id
        Start-Sleep -Seconds 1
    }

    # Save state
    $configData = Get-Content $CONFIG_FILE -Raw
    $TENANT  = _json_val -Json $configData -Key "tenant_id" -Default "default"
    $USER_ID = _json_val -Json $configData -Key "user_id"   -Default "default"

    # Write session state with PIDs
    if ($PYTHON_BIN) {
        $stateJson = @{
            active         = $true
            mode           = $MODE
            project_dir    = "$PROJECT_DIR"
            config_path    = $CONFIG_FILE
            http_url       = $HTTP_URL
            tenant_id      = $TENANT
            user_id        = $USER_ID
            http_pid       = [int]$HTTP_PID
            mcp_pid        = [int]$MCP_PID
            last_turn_uuid = ""
            ingested_turns = 0
            started_at     = [int](Get-Date -UFormat %s)
        } | ConvertTo-Json
        $stateJson | Set-Content -Path $STATE_FILE -Encoding UTF8
    }

    $STATUS = "[opencortex-memory] local mode - HTTP :$HTTP_PORT MCP :$MCP_PORT tenant=$TENANT user=$USER_ID"

# ---------------------------------------------------------------------------
# Remote mode: verify connectivity
# ---------------------------------------------------------------------------
} else {
    $REMOTE_HTTP = Get-PluginConfig -Key "remote.http_url" -Default ""
    if (-not $REMOTE_HTTP) {
        $msg = "[opencortex-memory] WARNING: remote.http_url not configured in config.json"
        $jsonMsg = _json_encode_str $msg
        Write-Output "{`"systemMessage`": $jsonMsg}"
        exit 0
    }

    # Test connectivity
    $reachable = $false
    try {
        $null = Invoke-RestMethod -Uri "$REMOTE_HTTP/api/v1/memory/health" -TimeoutSec 3 -ErrorAction Stop
        $reachable = $true
    } catch {}

    if (-not $reachable) {
        $msg = "[opencortex-memory] WARNING: remote HTTP server unreachable at $REMOTE_HTTP"
        $jsonMsg = _json_encode_str $msg
        Write-Output "{`"systemMessage`": $jsonMsg}"
        exit 0
    }

    $configData = Get-Content $CONFIG_FILE -Raw
    $TENANT  = _json_val -Json $configData -Key "tenant_id" -Default "default"
    $USER_ID = _json_val -Json $configData -Key "user_id"   -Default "default"

    # Write session state (no PIDs for remote mode)
    $stateJson = @{
        active         = $true
        mode           = "remote"
        project_dir    = "$PROJECT_DIR"
        config_path    = $CONFIG_FILE
        http_url       = $REMOTE_HTTP
        tenant_id      = $TENANT
        user_id        = $USER_ID
        http_pid       = 0
        mcp_pid        = 0
        last_turn_uuid = ""
        ingested_turns = 0
        started_at     = [int](Get-Date -UFormat %s)
    } | ConvertTo-Json
    $stateJson | Set-Content -Path $STATE_FILE -Encoding UTF8

    $STATUS = "[opencortex-memory] remote mode - $REMOTE_HTTP tenant=$TENANT user=$USER_ID"
}

$jsonStatus = _json_encode_str $STATUS
Write-Output "{`"systemMessage`": $jsonStatus}"
