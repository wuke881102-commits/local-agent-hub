; Inno Setup script for Local Agent Hub
; Build: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\installer.iss

#define MyAppName "Local Agent Hub"
#define MyAppNameCN "本地 Agent 工作台"
#define MyAppVersion "5.4"
#define MyAppPublisher "Local Agent Hub"
#define MyAppExeName "feishu-agent.exe"
#define MyAppExeRelative "backend\feishu-agent.exe"

[Setup]
AppId={{2F1E5807-A6E2-4F1A-9B5C-FE15HU0HUB001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist-installer
OutputBaseFilename=LocalAgentHub-Setup-{#MyAppVersion}
Compression=lzma2/ultra
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; lowest = per-user install (no UAC); commandline override allows /ALLUSERS for machine-wide.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
WizardStyle=modern
UninstallDisplayName={#MyAppName} {#MyAppVersion}
UninstallDisplayIcon={app}\{#MyAppExeRelative}

[Languages]
; Chinese first → installer wizard defaults to Chinese; user can still pick English.
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
; Optional clean reset — wipes the per-user data dir before files are copied, so even an
; in-place upgrade (which never runs the uninstaller) can start from a blank index/task list.
Name: "resetdata"; Description: "重置本地数据(清空已有索引、任务、草稿、日志,从干净状态开始)"; Flags: unchecked

[Files]
; Bundle the whole staged tree. Order matters only for shortcut targets.
Source: "stage\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeRelative}"; WorkingDir: "{app}\backend"; Comment: "{#MyAppNameCN}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeRelative}"; WorkingDir: "{app}\backend"; Tasks: desktopicon; Comment: "{#MyAppNameCN}"

[Run]
Filename: "{app}\{#MyAppExeRelative}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; WorkingDir: "{app}\backend"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Best-effort kill any running instance so files can be removed.
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#MyAppExeName} >nul 2>&1"; Flags: runhidden; RunOnceId: "killbackend"

[UninstallDelete]
; Sweep any runtime data accidentally created inside the install tree.
Type: filesandordirs; Name: "{app}\backend\data"
Type: filesandordirs; Name: "{app}\backend\__pycache__"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

// If the "resetdata" task is checked, wipe %LOCALAPPDATA%\Feishu Agent Hub before
// copying files. That dir lives OUTSIDE {app}, so an in-place upgrade would otherwise
// keep the old index/tasks. We first kill any running instance so index.sqlite/logs
// aren't locked. Silent installs only reset if launched with /TASKS="resetdata".
procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
  ResultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    if WizardIsTaskSelected('resetdata') then
    begin
      Exec(ExpandConstant('{cmd}'), '/C taskkill /F /IM feishu-agent.exe',
           '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      DataDir := ExpandConstant('{localappdata}\Feishu Agent Hub');
      if DirExists(DataDir) then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;

// On uninstall, offer to also wipe the per-user data dir
// (%LOCALAPPDATA%\Feishu Agent Hub: index.sqlite, drafts, logs, optional .env).
// This dir lives OUTSIDE {app}, so a normal uninstall/reinstall would otherwise
// leave a tester's machine carrying stale index + task history. Prompting (default
// Yes) lets interactive users keep data, while silent uninstalls reset cleanly.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Feishu Agent Hub');
    if DirExists(DataDir) then
    begin
      if MsgBox('是否同时删除本地数据(索引、任务记录、草稿、日志)?' + #13#10 +
                DataDir + #13#10#13#10 +
                '选择"是"可让下次安装从干净状态开始;选择"否"则保留这些数据。',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
