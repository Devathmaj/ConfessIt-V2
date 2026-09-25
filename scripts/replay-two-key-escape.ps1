#Requires -Version 5.1
<#
.SYNOPSIS
    Wipe Two-Key Escape gameplay and deal a fresh maze, in one command.

.DESCRIPTION
    Replays the debug loop end to end: wipes every maze period and every
    couple's live game (scripts/wipe-two-key-escape.sql), then restarts the
    backend so boot deals a fresh period at the stored grid size, and waits
    until the new period is active.

    Keeps, by design: the grid-size config, the questionnaire pairings
    (couple_game_sessions + selections), users, and every other feature.
    The same two users re-submit roles and play again -- no re-pairing.

    Usage (from the repo root):
        .\scripts\replay-two-key-escape.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SqlFile = Join-Path $PSScriptRoot 'wipe-two-key-escape.sql'
$PgContainer = 'confessit_v2-postgres-1'
$BackendContainer = 'confessit_v2-backend-1'

function Invoke-Db([string]$Sql) {
    $env:PGPASSWORD = 'ConfessItLocalPassword'
    try {
        docker exec -i -e PGPASSWORD=ConfessItLocalPassword $PgContainer `
            psql -U confessit -d ConfessIt -t -A -c $Sql
    }
    finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

Write-Host '[1/3] Wiping Two-Key Escape gameplay...'
Get-Content -LiteralPath $SqlFile -Raw |
    docker exec -i -e PGPASSWORD=ConfessItLocalPassword $PgContainer `
        psql -U confessit -d ConfessIt -f - | Out-Null
$left = Invoke-Db "SELECT count(*) FROM two_key_escape_games;"
if ($left.Trim() -ne '0') { throw "Wipe failed: $left period row(s) remain." }
Write-Host '      gameplay wiped (pairings and grid-size config kept).'

Write-Host '[2/3] Restarting backend to deal a fresh maze...'
docker restart $BackendContainer | Out-Null

Write-Host '[3/3] Waiting for the fresh period...'
$deadline = (Get-Date).AddSeconds(120)
while ($true) {
    $row = Invoke-Db "SELECT status || '|' || grid_size FROM two_key_escape_games WHERE status IN ('active','pending') LIMIT 1;"
    if ($row -and $row.Trim() -ne '') {
        $status, $size = $row.Trim() -split '\|'
        Write-Host ("      fresh period is ${status}: ${size}x${size} maze.")
        break
    }
    if ((Get-Date) -gt $deadline) { throw 'Timed out waiting for the fresh period. Check backend logs.' }
    Start-Sleep -Seconds 3
}

Write-Host 'Done: open Two-Key Escape and play -- role picks start over, pairs do not.'
