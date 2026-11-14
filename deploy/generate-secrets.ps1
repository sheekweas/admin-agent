# Prints strong random values for deploy/.env.production (copy the lines you need).
# Hex values: safe inside URLs (DATABASE_URL / REDIS_URL). Nothing is written to disk.
# Windows PowerShell 5.1+ / PowerShell 7.
$ErrorActionPreference = 'Stop'

function New-RandomHex([int]$Bytes) {
    $buf = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buf) } finally { $rng.Dispose() }
    -join ($buf | ForEach-Object { $_.ToString('x2') })
}

"JWT_SECRET=$(New-RandomHex 32)"
"SECRETS_ENCRYPTION_KEY=$(New-RandomHex 32)"
"TELEGRAM_WEBHOOK_SECRET=$(New-RandomHex 24)"
"WHATSAPP_VERIFY_TOKEN=$(New-RandomHex 16)"
"METRICS_TOKEN=$(New-RandomHex 24)"
"POSTGRES_PASSWORD=$(New-RandomHex 24)"
"REDIS_PASSWORD=$(New-RandomHex 24)"
"BACKUP_PASSPHRASE=$(New-RandomHex 32)"
Write-Host '# WHATSAPP_APP_SECRET comes from Meta (App settings -> Basic), it is not generated here.'
