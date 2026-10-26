; 螢幕測錄系統（管理伺服器）—— 圖形安裝精靈（Inno Setup 6）
;
; 這支精靈只是「下一步、下一步」的外殼：收集安裝位置、連接埠、管理員帳號與選項，
; 其餘全部交給 server\install.ps1 完成（Python、TLS、資料庫、管理員、服務、防火牆、
; 以及可選的 .NET SDK + WiX 與 FFmpeg）。所有真正的安裝邏輯只住在 install.ps1 一處，
; 精靈不重複實作，日後維護只要改那一支。
;
; 編譯：server\packaging\Build-ServerSetup.ps1（會先把要打包的檔案備好再呼叫 ISCC）。
; 由 Build 腳本以 /DAppVer=... /DPayloadDir=... 傳入版本與打包內容的位置。

#ifndef AppVer
  #define AppVer "1.0.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "build\payload"
#endif

[Setup]
AppId={{B7E1F2A4-3C6D-4E59-9A17-2F8D5C0E4B31}
AppName=螢幕測錄系統 管理伺服器
AppVersion={#AppVer}
AppVerName=螢幕測錄系統 管理伺服器 {#AppVer}
AppPublisher=公司 IT 部門
DefaultDirName={sd}\EEM
DisableProgramGroupPage=yes
DisableWelcomePage=no
DisableDirPage=no
PrivilegesRequired=admin
OutputBaseFilename=EEMServerSetup-{#AppVer}
OutputDir=dist
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
InfoBeforeFile=notice.txt
UninstallDisplayName=螢幕測錄系統 管理伺服器
VersionInfoCompany=EEM
VersionInfoDescription=螢幕測錄系統 管理伺服器安裝程式
VersionInfoVersion={#AppVer}

[Files]
; 打包內容（server 原始碼、Agent 素材、docs）先解壓到暫存資料夾，install.ps1 再從那裡
; 佈署到 {app}。暫存資料夾在精靈結束時自動清掉，不會殘留。
Source: "{#PayloadDir}\*"; DestDir: "{tmp}\eemsrc"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\螢幕測錄系統\開啟主控台"; Filename: "{app}\主控台.url"
Name: "{autoprograms}\螢幕測錄系統\解除安裝螢幕測錄系統"; Filename: "{uninstallexe}"

[Run]
; 安裝成功才顯示「開啟主控台」勾選項；靜默安裝不跳瀏覽器。
Filename: "{code:GetConsoleUrl}"; Description: "開啟主控台"; Flags: shellexec postinstall skipifsilent nowait; Check: GetInstallOk

[UninstallDelete]
Type: files; Name: "{app}\主控台.url"
Type: files; Name: "{app}\setup-install.log"

[UninstallRun]
; 真正的移除工作交給 install.ps1 -Uninstall（透過 uninstall.ps1）。保留資料或連資料
; 一併刪除，由 InitializeUninstall 的詢問決定（見 [Code]）。視窗保持顯示，讓使用者看到
; 進度與任何提示。
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\server\uninstall.ps1"" -InstallDir ""{app}"" {code:GetUninstallArgs}"; Flags: waituntilterminated; RunOnceId: "EEMServerUninstall"

[Messages]
SetupAppTitle=安裝程式
SetupWindowTitle=%1 安裝程式
ButtonBack=< 上一步(&B)
ButtonNext=下一步(&N) >
ButtonInstall=安裝(&I)
ButtonCancel=取消
ButtonFinish=完成(&F)
ButtonBrowse=瀏覽(&R)...
ButtonWizardBrowse=瀏覽(&R)...
ButtonNewFolder=建立新資料夾(&M)
ExitSetupTitle=結束安裝程式
ExitSetupMessage=安裝尚未完成。若現在結束，伺服器將不會被安裝。%n%n可以稍後再執行一次安裝精靈。%n%n確定要結束嗎？
WelcomeLabel1=歡迎使用[name]安裝精靈
WelcomeLabel2=這個精靈會在這台電腦上架設[name]。%n%n建議先關閉其他應用程式再繼續。
ClickNext=按「下一步」繼續，或按「取消」結束安裝程式。
WizardInfoBefore=安裝前須知
InfoBeforeLabel=請在繼續之前閱讀以下重要資訊。
InfoBeforeClickLabel=準備好繼續安裝後，請按「下一步」。
WizardSelectDir=選擇安裝位置
SelectDirDesc=要將[name]安裝到哪裡？
SelectDirLabel3=安裝程式會把[name]的資料與程式放在下列資料夾（預設 C:\EEM）。
SelectDirBrowseLabel=按「下一步」繼續。若要指定其他資料夾，請按「瀏覽」。
DiskSpaceGBLabel=此程式至少需要 [gb] GB 可用磁碟空間。
DiskSpaceMBLabel=此程式至少需要 [mb] MB 可用磁碟空間。
WizardReady=準備安裝
ReadyLabel1=安裝程式已準備好開始安裝[name]。
ReadyLabel2a=按「安裝」開始安裝，或按「上一步」檢視或變更設定。
ReadyLabel2b=按「安裝」開始安裝。
WizardPreparing=準備中
PreparingDesc=安裝程式正在準備安裝[name]。
WizardInstalling=正在安裝
InstallingLabel=請稍候，安裝程式正在設定[name]。
StatusExtractFiles=正在解壓縮檔案...
FinishedHeadingLabel=[name]安裝完成
FinishedLabelNoIcons=安裝程式已完成[name]的安裝。
FinishedLabel=安裝程式已完成[name]的安裝。
ClickFinish=按「完成」結束安裝程式。
ConfirmUninstall=確定要移除螢幕測錄系統管理伺服器嗎？

[Code]
var
  SettingsPage: TInputQueryWizardPage;
  OptionsPage: TInputOptionWizardPage;
  GInstallOk: Boolean;
  GUrl, GLanUrl, GAdminUser, GAdminPassword, GCertKind, GLogFile: String;
  GRecording: Boolean;
  GUninstPurge: Boolean;

procedure InitializeWizard();
begin
  SettingsPage := CreateInputQueryPage(wpSelectDir,
    '伺服器設定', '設定主控台的連接埠與管理員帳號',
    '可直接使用預設值；安裝後仍可在設定檔 .env 調整。');
  SettingsPage.Add('HTTPS 連接埠（預設 443）：', False);
  SettingsPage.Add('最高管理員帳號（預設 admin）：', False);
  SettingsPage.Values[0] := '443';
  SettingsPage.Values[1] := 'admin';

  OptionsPage := CreateInputOptionPage(SettingsPage.ID,
    '安裝選項', '選擇要一併安裝的功能',
    '若不確定，建議全部保持勾選。缺少的元件會自動下載。', False, False);
  OptionsPage.Add('啟用螢幕錄影（安裝 FFmpeg，可能需下載約 100 MB）');
  OptionsPage.Add('在 Windows 防火牆放行此連接埠');
  OptionsPage.Add('安裝端點安裝包工具鏈（.NET SDK + WiX，讓主控台能自製 Agent 安裝包）');
  OptionsPage.Values[0] := True;
  OptionsPage.Values[1] := True;
  OptionsPage.Values[2] := True;
end;

function IsValidPort(S: String): Boolean;
var
  n, i: Integer;
begin
  Result := False;
  if (Length(S) = 0) or (Length(S) > 5) then Exit;
  for i := 1 to Length(S) do
    if (S[i] < '0') or (S[i] > '9') then Exit;
  n := StrToIntDef(S, -1);
  Result := (n >= 1) and (n <= 65535);
end;

function IsValidUser(S: String): Boolean;
var
  i: Integer;
  c: Char;
begin
  Result := (Length(S) >= 1) and (Length(S) <= 32);
  if not Result then Exit;
  for i := 1 to Length(S) do
  begin
    c := S[i];
    if not (((c >= 'a') and (c <= 'z')) or ((c >= 'A') and (c <= 'Z')) or
            ((c >= '0') and (c <= '9')) or (c = '_') or (c = '-') or (c = '.')) then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = SettingsPage.ID then
  begin
    if not IsValidPort(Trim(SettingsPage.Values[0])) then
    begin
      MsgBox('連接埠必須是 1 到 65535 之間的數字。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if not IsValidUser(Trim(SettingsPage.Values[1])) then
    begin
      MsgBox('管理員帳號只能使用英數字與 _ - . ，長度 1 到 32。', mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end;
end;

function HasParam(const Name: String): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), Name) = 0 then
    begin
      Result := True;
      Exit;
    end;
end;

// 設定來源優先序：圖形頁面（互動安裝）→ 命令列 /PARAM（靜默安裝）→ 內建預設值。
// 靜默安裝（/VERYSILENT）不會呼叫 InitializeWizard，頁面會是 nil，所以這裡都要容錯，
// 並支援 /PORT= /ADMINUSER= 與 /NORECORDING /NOFIREWALL /NOPACKAGING 無人值守佈署。
function CfgPort(): String;
begin
  if SettingsPage <> nil then
    Result := Trim(SettingsPage.Values[0])
  else
    Result := ExpandConstant('{param:PORT|443}');
  if Result = '' then Result := '443';
end;

function CfgUser(): String;
begin
  if SettingsPage <> nil then
    Result := Trim(SettingsPage.Values[1])
  else
    Result := ExpandConstant('{param:ADMINUSER|admin}');
  if Result = '' then Result := 'admin';
end;

function OptRecording(): Boolean;
begin
  if OptionsPage <> nil then Result := OptionsPage.Values[0]
  else Result := not HasParam('/NORECORDING');
end;

function OptFirewall(): Boolean;
begin
  if OptionsPage <> nil then Result := OptionsPage.Values[1]
  else Result := not HasParam('/NOFIREWALL');
end;

function OptPackaging(): Boolean;
begin
  if OptionsPage <> nil then Result := OptionsPage.Values[2]
  else Result := not HasParam('/NOPACKAGING');
end;

function GetConsoleUrl(Param: String): String;
begin
  if GUrl <> '' then
    Result := GUrl
  else
    Result := 'https://localhost';
end;

function GetInstallOk(): Boolean;
begin
  Result := GInstallOk;
end;

// 在檔案複製後、執行 install.ps1 前就把開啟主控台的捷徑建好（捷徑指向它）。
// 這時還沒有結果檔，網址直接用使用者選的連接埠推算。
procedure MakeConsoleShortcut();
var
  port, url: String;
begin
  port := CfgPort();
  if port = '443' then
    url := 'https://localhost'
  else
    url := 'https://localhost:' + port;
  SaveStringToFile(ExpandConstant('{app}\主控台.url'),
    '[InternetShortcut]' + #13#10 + 'URL=' + url + #13#10, False);
end;

procedure RunInstaller();
var
  SrcDir, ScriptPath, AppDir, ResultFile, CmdFile, Cmd, Switches, Port, AdminUser: String;
  rc: Integer;
begin
  SrcDir := ExpandConstant('{tmp}\eemsrc');
  ScriptPath := SrcDir + '\server\install.ps1';
  AppDir := ExpandConstant('{app}');
  ResultFile := ExpandConstant('{tmp}\eem-result.ini');
  GLogFile := ExpandConstant('{tmp}\eem-setup-install.log');
  Port := CfgPort();
  AdminUser := CfgUser();

  Switches := '';
  if not OptRecording() then Switches := Switches + ' -NoRecording';
  if not OptFirewall() then Switches := Switches + ' -NoFirewall';
  if not OptPackaging() then Switches := Switches + ' -NoPackaging';

  // 用產生出來的批次檔避免 Inno 這層再被引號糾纏；PowerShell 內以單引號包路徑。
  // Start-Transcript 讓安裝過程同時顯示在視窗並寫進記錄檔；成敗改看結果檔的 healthy，
  // 不靠行程結束碼（管線經過 Tee/Transcript 會蓋掉真正的結束碼）。
  Cmd :=
    '@echo off' + #13#10 +
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' +
      '$ErrorActionPreference=''Continue''; ' +
      'Start-Transcript -LiteralPath ''' + GLogFile + ''' -Force | Out-Null; ' +
      'try { & ''' + ScriptPath + ''' -InstallDir ''' + AppDir + ''' -Port ' + Port +
        ' -AdminUser ''' + AdminUser + ''' -ResultFile ''' + ResultFile + '''' + Switches +
      ' } finally { Stop-Transcript | Out-Null }' +
    '"' + #13#10;
  CmdFile := ExpandConstant('{tmp}\eem-run-install.cmd');
  SaveStringToFile(CmdFile, Cmd, False);

  WizardForm.StatusLabel.Caption :=
    '正在安裝並設定伺服器…會下載 Python / .NET / FFmpeg，請在另一個視窗查看進度（可能需要數分鐘）。';
  WizardForm.FilenameLabel.Caption := '';

  if not Exec(ExpandConstant('{cmd}'), '/c "' + CmdFile + '"', '', SW_SHOW, ewWaitUntilTerminated, rc) then
    rc := -1;

  GInstallOk := FileExists(ResultFile) and (GetIniString('result', 'healthy', '0', ResultFile) = '1');
  if FileExists(ResultFile) then
  begin
    GUrl := GetIniString('result', 'url', '', ResultFile);
    GLanUrl := GetIniString('result', 'lanUrl', '', ResultFile);
    GAdminUser := GetIniString('result', 'adminUser', '', ResultFile);
    GAdminPassword := GetIniString('result', 'adminPassword', '', ResultFile);
    GCertKind := GetIniString('result', 'certKind', '', ResultFile);
    GRecording := GetIniString('result', 'recording', '0', ResultFile) = '1';
    DeleteFile(ResultFile);   // 密碼不留在磁碟
  end;

  if not GInstallOk then
  begin
    // 失敗：把安裝記錄複製到安裝目錄，方便事後排查（成功時記錄隨暫存資料夾清除）。
    if DirExists(AppDir) or ForceDirectories(AppDir) then
      CopyFile(GLogFile, AppDir + '\setup-install.log', False);
    MsgBox('安裝未完成，伺服器可能沒有正常啟動。' + #13#10#13#10 +
      '安裝記錄：' + AppDir + '\setup-install.log' + #13#10#13#10 +
      '請修正後重新執行本安裝精靈（資料會保留）。', mbError, MB_OK);
  end;
end;

// 強制彈窗顯示登入資訊：完成頁的說明區會被「開啟主控台」勾選框壓縮而截斷文字，
// 所以密碼另外用一個非關不可的 MsgBox 顯示（可按 Ctrl+C 複製整個視窗文字）。
procedure ShowCredentials();
var
  M: String;
begin
  M := '伺服器安裝完成，請立刻記下登入資訊（只顯示這一次）：' + #13#10#13#10;
  M := M + '網址：' + GUrl + #13#10;
  if GLanUrl <> '' then
    M := M + '區域網路：' + GLanUrl + #13#10;
  M := M + '帳號：' + GAdminUser + #13#10;
  if GAdminPassword <> '' then
    M := M + '密碼：' + GAdminPassword + #13#10#13#10 +
             '提示：按 Ctrl+C 可複製本視窗的文字。'
  else
    M := M + '密碼：使用你安裝時指定的 EEM_ADMIN_PASSWORD。' + #13#10;
  MsgBox(M, mbInformation, MB_OK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    MakeConsoleShortcut()
  else if CurStep = ssPostInstall then
  begin
    RunInstaller();
    // 安裝成功且非靜默時，強制彈窗把帳密秀出來（完成頁可能截斷）。
    if GInstallOk and (not WizardSilent) then
      ShowCredentials();
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
var
  S: String;
begin
  if CurPageID = wpFinished then
  begin
    if GInstallOk then
    begin
      // 帳密放最上面：完成頁說明區會被下方的勾選框壓縮，長文字會從底部截斷，
      // 所以把最重要的帳密擺在前兩行，確保一定看得到（另有 ShowCredentials 彈窗保底）。
      S := '安裝完成，伺服器已啟動。' + #13#10#13#10;
      S := S + '管理員帳號：' + GAdminUser + #13#10;
      if GAdminPassword <> '' then
        S := S + '管理員密碼：' + GAdminPassword + '　← 只顯示這一次，請立刻保存' + #13#10#13#10
      else
        S := S + '管理員密碼：使用安裝時指定的 EEM_ADMIN_PASSWORD' + #13#10#13#10;
      S := S + '主控台網址：' + GUrl + #13#10;
      if GLanUrl <> '' then
        S := S + '（區域網路：' + GLanUrl + '）' + #13#10;
      if not GRecording then
        S := S + #13#10 + '註：未啟用螢幕錄影（沒有可用的 FFmpeg）。';
      if GCertKind = 'self-signed' then
        S := S + #13#10 + '註：使用自簽憑證，瀏覽器出現安全警告屬正常。';
      WizardForm.FinishedLabel.Caption := S;
    end
    else
    begin
      WizardForm.FinishedLabel.Caption :=
        '安裝未完成，伺服器可能尚未啟動。' + #13#10#13#10 +
        '請查看安裝記錄：' + ExpandConstant('{app}') + '\setup-install.log' + #13#10 +
        '修正後重新執行安裝精靈即可（資料會保留）。';
    end;
  end;
end;

// ---- 解除安裝：先問要不要連同資料（資料庫、錄影、金鑰）一併刪除 ----

function InitializeUninstall(): Boolean;
var
  r: Integer;
begin
  Result := True;
  GUninstPurge := False;
  r := MsgBox('要移除螢幕測錄系統管理伺服器。' + #13#10#13#10 +
    '是否連同「資料庫、所有錄影與截圖、加密金鑰、TLS 憑證」一併刪除？' + #13#10#13#10 +
    '• 「是」＝全部刪除，無法復原。' + #13#10 +
    '• 「否」＝只停用服務並移除防火牆規則，資料保留（之後重裝可復原）。' + #13#10 +
    '• 「取消」＝不移除。',
    mbConfirmation, MB_YESNOCANCEL);
  if r = IDCANCEL then
    Result := False
  else if r = IDYES then
    GUninstPurge := True;
end;

function GetUninstallArgs(Param: String): String;
begin
  if GUninstPurge then
    Result := '-Force'          // 連資料一併刪除（已在上面確認過，略過 DELETE 二次確認）
  else
    Result := '-KeepData';      // 只停服務＋防火牆，資料保留
end;
