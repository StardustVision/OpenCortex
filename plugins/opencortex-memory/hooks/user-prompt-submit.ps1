# UserPromptSubmit hook: inject systemMessage prompting Claude to use
# memory_search MCP tool when context recall would be helpful.
# This hook is intentionally lightweight — no subprocess calls, no HTTP requests.

. "$PSScriptRoot\common.ps1"

$PROMPT = _json_val -Json $INPUT -Key "prompt" -Default ""
if (-not $PROMPT) {
    Write-Output '{}'
    exit 0
}

# Only inject if session is active (config + state exist)
if (-not $CONFIG_FILE -or -not (Test-Path $CONFIG_FILE) -or -not (Test-Path $STATE_FILE)) {
    Write-Output '{}'
    exit 0
}

# Check session is active
$stateContent = Get-Content $STATE_FILE -Raw
$ACTIVE = _json_val -Json $stateContent -Key "active" -Default "false"
if ($ACTIVE -ne "true") {
    Write-Output '{}'
    exit 0
}

Write-Output '{"systemMessage": "[opencortex-memory] Memory system active. If this query could benefit from past context, preferences, or learned patterns, use the memory_search MCP tool."}'
