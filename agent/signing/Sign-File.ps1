<#
.SYNOPSIS
    Authenticode-sign a file (.exe or .msi) with the internal signing certificate.

.DESCRIPTION
    Used both by agent/build.ps1 (to sign the agent binary once) and by the
    server's package generator (to sign each generated MSI). Uses the built-in
    Set-AuthenticodeSignature, so no Windows SDK / signtool install is required.

    The .pfx password is read from the EEM_SIGNING_PASSWORD environment variable,
    never a command-line argument -- an argument would be visible in the process
    list to any local user.

    Timestamping is applied when a URL is given, so signatures stay valid after
    the certificate expires. It needs outbound network; omit it for a fully
    offline internal signer and re-sign before the cert expires instead.

.EXAMPLE
    $env:EEM_SIGNING_PASSWORD = "..."
    ./Sign-File.ps1 -File agent.msi -CertPath C:\secrets\endpoint-agent-signing.pfx `
                    -TimestampUrl http://timestamp.digicert.com
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$File,
    [Parameter(Mandatory)][string]$CertPath,
    [string]$TimestampUrl = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $File))     { throw "File not found: $File" }
if (-not (Test-Path $CertPath)) { throw "Certificate not found: $CertPath" }

$pw = $env:EEM_SIGNING_PASSWORD
if ([string]::IsNullOrEmpty($pw)) {
    throw "EEM_SIGNING_PASSWORD is not set. Provide the .pfx password via that environment variable."
}

$securePw = ConvertTo-SecureString $pw -AsPlainText -Force
# X509KeyStorageFlags.EphemeralKeySet keeps the private key out of any store.
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
    $CertPath, $securePw,
    [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)

$params = @{
    FilePath      = $File
    Certificate   = $cert
    HashAlgorithm = "SHA256"
}
if (-not [string]::IsNullOrEmpty($TimestampUrl)) {
    $params["TimestampServer"] = $TimestampUrl
}

$result = Set-AuthenticodeSignature @params

if ($result.Status -ne "Valid" -and $result.Status -ne "UnknownError") {
    # UnknownError here means "signed, but the self-signed root is not trusted on
    # THIS machine" -- expected for an internal cert on the build server. A real
    # failure (HashMismatch, NotSigned) is what we reject.
    throw "Signing failed: $($result.Status) - $($result.StatusMessage)"
}

Write-Host "Signed $File"
Write-Host "  signer : $($result.SignerCertificate.Subject)"
Write-Host "  status : $($result.Status)  ($($result.StatusMessage))"
if ($result.TimeStamperCertificate) {
    Write-Host "  timestamped: yes"
}
