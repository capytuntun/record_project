<#
.SYNOPSIS
    Create a self-signed code-signing certificate for internal deployment.

.DESCRIPTION
    For a purely internal RMM -- machines you manage by GPO/MDM -- you do not
    need a certificate from a public CA. You sign with your own certificate and
    distribute its PUBLIC half to the Trusted Publishers store on managed
    machines via Group Policy. Once that is in place, SmartScreen and AppLocker
    accept your signed agent and packages.

    This produces two files:
      * <name>.cer  -- the PUBLIC certificate. Safe to share. Distribute via GPO.
      * <name>.pfx  -- the PRIVATE key, password-protected. This is the signing
                       secret. Keep it OUT of the repo and off shared drives;
                       give the server access to it as a protected secret only.

.EXAMPLE
    ./New-SigningCert.ps1 -Subject "CN=Contoso Endpoint Agent" -OutDir C:\secrets
#>
[CmdletBinding()]
param(
    [string]$Subject = "CN=Enterprise IT Endpoint Agent Signing",
    [int]$ValidYears = 5,
    [string]$OutDir = ".",
    [string]$Name = "endpoint-agent-signing"
)

$ErrorActionPreference = "Stop"

$password = Read-Host -AsSecureString "Set a password for the .pfx (you will need it to sign)"

$cert = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $Subject `
    -KeyUsage DigitalSignature `
    -KeyAlgorithm RSA -KeyLength 3072 `
    -HashAlgorithm SHA256 `
    -KeyExportPolicy Exportable `
    -NotAfter (Get-Date).AddYears($ValidYears) `
    -CertStoreLocation Cert:\CurrentUser\My

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$cerPath = Join-Path $OutDir "$Name.cer"
$pfxPath = Join-Path $OutDir "$Name.pfx"

Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $password | Out-Null

# Remove the key from the personal store; the .pfx is now the system of record.
Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)" -Force

Write-Host "Thumbprint : $($cert.Thumbprint)"
Write-Host "Public cert: $cerPath   (distribute via GPO -> Trusted Publishers)"
Write-Host "Private pfx: $pfxPath   (KEEP SECRET -- this signs your software)"
Write-Host ""
Write-Host "Next:"
Write-Host "  * Configure the server: EEM_SIGNING_PFX=$pfxPath and EEM_SIGNING_PASSWORD=<pw>"
Write-Host "  * See signing/README.md for GPO distribution of the .cer"
