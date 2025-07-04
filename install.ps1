echo "檢查python 環境變數"
$path = "C:\Users\$env:USERNAME\AppData\Local\Microsoft\WindowsApps\python.exe"
if (Test-Path $path) {
    Write-Output "WindowsApps python.exe 出現了。"
    Remove-Item $path -Force
    try {
        $pythonPath = Get-Command python | Select-Object -ExpandProperty Path
        Write-Output "Python executable path: $pythonPath"
        
        if ($pythonPath -like "*Microsoft\WindowsApps\python*") {
            Write-Output "Python path contains 'WindowsApps\python'. Removing from Path environment variable..."
            $env:Path = $env:Path -replace ";C:\\Users\\user\\AppData\\Local\\Microsoft\\WindowsApps", ""
        } else {
            Write-Output "Python path does not contain 'WindowsApps\python'."
        }
    } catch {
        Write-Output "並未出現$path"
    }
} else {
    Write-Output "WindowsApps python.exe 沒有出現"
}



echo "建立腳本目錄"
New-Item -Path "C:\Users\Public" -Name "python_script" -ItemType "directory"

echo "複製腳本"
Copy-Item $PSScriptRoot\code\python_recorder.py C:\Users\Public\python_script
Copy-Item $PSScriptRoot\code\app.py C:\Users\Public\python_script

echo "建立開機繳腳本"
Copy-Item $PSScriptRoot\startup_script\autostart.bat "$env:USERPROFILE\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"
Copy-Item $PSScriptRoot\startup_script\record-test.bat "$env:USERPROFILE\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"

echo "安裝 python 3.11.5"
Start-Process -FilePath $PWD\install\python-3.11.5-amd64.exe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine")
echo "安裝相關套件"
echo $PWD\install\requirements.txt
pip3 install -r $PWD\install\requirements.txt 

echo "建立pcinfoadmin帳戶"
New-LocalUser -Name "pcinfoadmin" -Password (ConvertTo-SecureString "~~1qaz2wsx" -AsPlainText -Force) -AccountNeverExpires
add-LocalGroupMember -Group "Administrators" -Member "pcinfoadmin"

echo "將user移出管理員群組"
Remove-LocalGroupMember -Group "Administrators" -Member $env:USERNAME

$userName = "user"

# 檢查該帳號是否存在
$existingUser = Get-LocalUser -Name $userName -ErrorAction SilentlyContinue

# 如果帳號不存在，則創建帳號
if (-not $existingUser) {
    Write-Host "使用者帳號不存在，正在創建帳號..."
    # 創建新帳號
    New-LocalUser -Name $userName -Password (ConvertTo-SecureString "1qaz@WSX" -AsPlainText -Force) -FullName "User Account" -Description "Created via script"
    Write-Host "帳號創建完成：$userName"
}

# 檢查該帳號是否在管理員群組內
if (Get-LocalGroupMember -Group "Administrators" | Where-Object { $_.Name -eq $userName }) {
    Write-Host "將 $userName 移出管理員群組..."
    Remove-LocalGroupMember -Group "Administrators" -Member $userName
    Write-Host "$userName 已被移出管理員群組"
} else {
    Write-Host "$userName 不在管理員群組內"
}

echo "將user加入Users群組" 
Add-LocalGroupMember -Group "Users" -Member $env:USERNAME


echo "重新開機"
Restart-Computer
