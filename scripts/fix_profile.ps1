chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$src = "W:\_python\APIPROXY\Profile 2"
$dst = "W:\_python\APIPROXY\Profile_Fixed"
Write-Host "Creating fixed profile directory..."
New-Item -ItemType Directory -Force -Path "$dst\Default" | Out-Null
if (Test-Path "$src\Local State") {
    Copy-Item "$src\Local State" -Destination "$dst\" -Force
}
Get-ChildItem $src | Where-Object { $_.Name -ne 'Local State' -and $_.Name -ne 'Default' } | Copy-Item -Destination "$dst\Default\" -Recurse -Force
Write-Host "Profile restructuring complete."
