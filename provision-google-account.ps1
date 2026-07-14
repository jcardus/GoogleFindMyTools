# Provision an existing Google account for the GoogleFindMyTools hub.
#
# This script does not create a Google/Gmail account. Create the regular Google
# account manually first, for example on a factory-reset Android phone, then run
# this from Windows PowerShell 5.1 or PowerShell 7+.
[CmdletBinding()]
param(
    [string]$GoogleAccount,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$Python = "python",
    [string]$SecretsFile,
    [switch]$RunAuth,
    [switch]$SkipOwnerKey,
    [switch]$Yes,
    [switch]$BackupExisting,

    [string]$SupabaseUrl = $env:SUPABASE_URL,
    [string]$SupabaseServiceKey = $(if ($env:SUPABASE_SERVICE_ROLE) { $env:SUPABASE_SERVICE_ROLE } else { $env:SUPABASE_SERVICE_KEY }),

    [string]$R2Bucket = $env:GOOGLE_SECRETS_R2_BUCKET,
    [string]$R2AccountId = $env:GOOGLE_SECRETS_R2_ACCOUNT_ID,
    [string]$R2AccessKeyId = $env:GOOGLE_SECRETS_R2_ACCESS_KEY_ID,
    [string]$R2SecretAccessKey = $env:GOOGLE_SECRETS_R2_SECRET_ACCESS_KEY,
    [string]$R2Endpoint = $env:GOOGLE_SECRETS_R2_ENDPOINT,
    [string]$R2Prefix = $(if ($env:GOOGLE_SECRETS_R2_PREFIX) { $env:GOOGLE_SECRETS_R2_PREFIX } else { "google-secrets" }),

    [string]$Status = "ready",
    [string]$Notes = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-Value([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Name is required. Pass -$Name or set the matching environment variable."
    }
}

function ConvertTo-Hex([byte[]]$Bytes) {
    -join ($Bytes | ForEach-Object { $_.ToString("x2") })
}

function Get-Sha256Hex([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        ConvertTo-Hex ($sha.ComputeHash($Bytes))
    }
    finally {
        $sha.Dispose()
    }
}

function Get-HmacSha256([byte[]]$Key, [string]$Data) {
    $hmac = [System.Security.Cryptography.HMACSHA256]::new($Key)
    try {
        $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Data))
    }
    finally {
        $hmac.Dispose()
    }
}

function Get-AwsSignatureKey([string]$Secret, [string]$DateStamp, [string]$Region, [string]$Service) {
    $kSecret = [System.Text.Encoding]::UTF8.GetBytes("AWS4$Secret")
    $kDate = Get-HmacSha256 $kSecret $DateStamp
    $kRegion = Get-HmacSha256 $kDate $Region
    $kService = Get-HmacSha256 $kRegion $Service
    Get-HmacSha256 $kService "aws4_request"
}

function ConvertTo-S3PathSegment([string]$Segment) {
    [System.Uri]::EscapeDataString($Segment).Replace("%2F", "/")
}

function Set-JsonProperty($InputObject, [string]$Name, $Value) {
    if ($InputObject.PSObject.Properties.Name -contains $Name) {
        $InputObject.$Name = $Value
    }
    else {
        $InputObject | Add-Member -NotePropertyName $Name -NotePropertyValue $Value
    }
}

function Get-SecretsUsername([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }

    try {
        $json = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    catch {
        throw "Could not parse existing secrets file: $Path"
    }

    if ($json.PSObject.Properties.Name -contains "username" -and -not [string]::IsNullOrWhiteSpace($json.username)) {
        return $json.username.Trim().ToLowerInvariant()
    }
    return $null
}

function Assert-CompatiblePython([string]$PythonCommand) {
    try {
        $versionOutput = & $PythonCommand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"
    }
    catch {
        throw "Could not run Python command '$PythonCommand'. Install Python 3.12, activate the venv, or pass -Python with the full path."
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not run Python command '$PythonCommand'. Install Python 3.12, activate the venv, or pass -Python with the full path."
    }

    $version = [Version]$versionOutput.Trim()
    if ($version.Major -ne 3 -or $version.Minor -ne 12) {
        throw "Unsupported Python $version. Use Python 3.12."
    }
}

function Write-R2Object {
    param(
        [Parameter(Mandatory = $true)][string]$Bucket,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][byte[]]$Body,
        [Parameter(Mandatory = $true)][string]$AccountId,
        [Parameter(Mandatory = $true)][string]$AccessKeyId,
        [Parameter(Mandatory = $true)][string]$SecretAccessKey,
        [string]$Endpoint
    )

    if ([string]::IsNullOrWhiteSpace($Endpoint)) {
        $Endpoint = "https://$AccountId.r2.cloudflarestorage.com"
    }

    $baseUri = [System.Uri]$Endpoint.TrimEnd("/")
    $hostName = $baseUri.Host
    $region = "auto"
    $service = "s3"
    $method = "PUT"
    $contentType = "application/json"
    $escapedKey = (($Key -split "/") | ForEach-Object { ConvertTo-S3PathSegment $_ }) -join "/"
    $canonicalUri = "/" + (ConvertTo-S3PathSegment $Bucket) + "/" + $escapedKey
    $uri = "$($baseUri.Scheme)://$hostName$canonicalUri"
    $now = [DateTime]::UtcNow
    $amzDate = $now.ToString("yyyyMMddTHHmmssZ")
    $dateStamp = $now.ToString("yyyyMMdd")
    $payloadHash = Get-Sha256Hex $Body

    $canonicalHeaders = "content-type:$contentType`nhost:$hostName`nx-amz-content-sha256:$payloadHash`nx-amz-date:$amzDate`n"
    $signedHeaders = "content-type;host;x-amz-content-sha256;x-amz-date"
    $canonicalRequest = "$method`n$canonicalUri`n`n$canonicalHeaders`n$signedHeaders`n$payloadHash"
    $credentialScope = "$dateStamp/$region/$service/aws4_request"
    $stringToSign = "AWS4-HMAC-SHA256`n$amzDate`n$credentialScope`n$(Get-Sha256Hex ([System.Text.Encoding]::UTF8.GetBytes($canonicalRequest)))"
    $signingKey = Get-AwsSignatureKey $SecretAccessKey $dateStamp $region $service
    $signature = ConvertTo-Hex (Get-HmacSha256 $signingKey $stringToSign)
    $authorization = "AWS4-HMAC-SHA256 Credential=$AccessKeyId/$credentialScope, SignedHeaders=$signedHeaders, Signature=$signature"

    Invoke-RestMethod -Method Put -Uri $uri -Body $Body -ContentType $contentType -Headers @{
        "Authorization" = $authorization
        "x-amz-content-sha256" = $payloadHash
        "x-amz-date" = $amzDate
    } | Out-Null
}

$toolsRoot = Join-Path $RepoRoot "GoogleFindMyTools"
if (-not (Test-Path -LiteralPath $toolsRoot)) {
    $toolsRoot = $PSScriptRoot
}
if (-not $SecretsFile) {
    if (-not [string]::IsNullOrWhiteSpace($GoogleAccount)) {
        $accountFileName = $GoogleAccount.Trim().ToLowerInvariant()
        $SecretsFile = Join-Path $toolsRoot (Join-Path "Auth" "$accountFileName.json")
    }
    else {
        $SecretsFile = Join-Path $toolsRoot (Join-Path "Auth" "secrets.json")
    }
}

if ([string]::IsNullOrWhiteSpace($GoogleAccount)) {
    $GoogleAccount = Get-SecretsUsername $SecretsFile
}
else {
    $GoogleAccount = $GoogleAccount.Trim().ToLowerInvariant()
}

if ([string]::IsNullOrWhiteSpace($GoogleAccount)) {
    if (-not $RunAuth) {
        throw "GoogleAccount is required when the secrets file does not contain username: $SecretsFile"
    }
}

if (-not [string]::IsNullOrWhiteSpace($GoogleAccount) -and $GoogleAccount -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") {
    throw "GoogleAccount must be an email address."
}

if ($RunAuth) {
    Assert-CompatiblePython $Python
    $authHelper = Join-Path $PSScriptRoot "provision_account_auth.py"
    $args = @($authHelper, "--secrets-file", $SecretsFile)
    if (-not [string]::IsNullOrWhiteSpace($GoogleAccount)) {
        $args += @("--google-account", $GoogleAccount)
    }
    if ($SkipOwnerKey) {
        $args += "--skip-owner-key"
    }
    if ($Yes) {
        $args += "--yes"
    }
    if ($BackupExisting) {
        $args += "--backup-existing"
    }
    Push-Location $toolsRoot
    try {
        & $Python @args
        if ($LASTEXITCODE -ne 0) {
            throw "Auth helper failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $SecretsFile)) {
    throw "Secrets file not found: $SecretsFile. Run with -RunAuth or pass -SecretsFile."
}

if ([string]::IsNullOrWhiteSpace($GoogleAccount)) {
    $GoogleAccount = Get-SecretsUsername $SecretsFile
}

if ([string]::IsNullOrWhiteSpace($GoogleAccount)) {
    throw "Could not infer GoogleAccount from secrets file after auth: $SecretsFile"
}

if ($GoogleAccount -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") {
    throw "GoogleAccount must be an email address."
}

Assert-Value "SupabaseUrl" $SupabaseUrl
Assert-Value "SupabaseServiceKey" $SupabaseServiceKey
Assert-Value "R2Bucket" $R2Bucket
Assert-Value "R2AccountId" $R2AccountId
Assert-Value "R2AccessKeyId" $R2AccessKeyId
Assert-Value "R2SecretAccessKey" $R2SecretAccessKey

$rawJson = Get-Content -LiteralPath $SecretsFile -Raw
$secretObject = $rawJson | ConvertFrom-Json
Set-JsonProperty $secretObject "username" $GoogleAccount
$uploadJson = $secretObject | ConvertTo-Json -Depth 20 -Compress
$body = [System.Text.Encoding]::UTF8.GetBytes($uploadJson)

$prefix = $R2Prefix.Trim("/")
$r2Key = if ($prefix) { "$prefix/$GoogleAccount.json" } else { "$GoogleAccount.json" }

Write-Host "Uploading auth cache to R2: $R2Bucket/$r2Key"
Write-R2Object -Bucket $R2Bucket -Key $r2Key -Body $body -AccountId $R2AccountId -AccessKeyId $R2AccessKeyId -SecretAccessKey $R2SecretAccessKey -Endpoint $R2Endpoint

$now = [DateTime]::UtcNow.ToString("o")
$payload = @{
    google_account = $GoogleAccount
    secrets_r2_bucket = $R2Bucket
    secrets_r2_key = $r2Key
    status = $Status
    notes = $Notes
    updated_at = $now
} | ConvertTo-Json -Depth 5

$supabaseEndpoint = "$($SupabaseUrl.TrimEnd('/'))/rest/v1/google_accounts?on_conflict=google_account"
Write-Host "Upserting Supabase google_accounts row for $GoogleAccount"
Invoke-RestMethod -Method Post -Uri $supabaseEndpoint -Body $payload -ContentType "application/json" -Headers @{
    "apikey" = $SupabaseServiceKey
    "Authorization" = "Bearer $SupabaseServiceKey"
    "Prefer" = "resolution=merge-duplicates,return=representation"
} | ConvertTo-Json -Depth 10

Write-Host "Done."
