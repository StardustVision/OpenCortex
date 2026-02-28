# Stop hook (async): ingest latest turn into OpenCortex memory.
# This runs after each assistant response completes.

. "$PSScriptRoot\common.ps1"

$STOP_HOOK_ACTIVE = _json_val -Json $INPUT -Key "stop_hook_active" -Default "false"
if ($STOP_HOOK_ACTIVE -eq "true") {
    Write-Output '{}'
    exit 0
}

if (-not $CONFIG_FILE -or -not (Test-Path $CONFIG_FILE) -or -not (Test-Path $STATE_FILE)) {
    Write-Output '{}'
    exit 0
}

$TRANSCRIPT_PATH = _json_val -Json $INPUT -Key "transcript_path" -Default ""
if (-not $TRANSCRIPT_PATH -or -not (Test-Path $TRANSCRIPT_PATH)) {
    Write-Output '{}'
    exit 0
}

# Fire-and-forget: start ingest as a detached background process.
$ingestArgs = "`"$BRIDGE`" --project-dir `"$PROJECT_DIR`" --state-file `"$STATE_FILE`" --config `"$CONFIG_FILE`" ingest-stop --transcript-path `"$TRANSCRIPT_PATH`""
Start-Process -FilePath $PYTHON_BIN -ArgumentList $ingestArgs `
    -NoNewWindow -WindowStyle Hidden

Write-Output '{}'
