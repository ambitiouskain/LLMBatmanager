#define MyAppName "LLMBatDesk"
#define MyAppVersion "1.3.4"
#define MyAppPublisher "LLMBatDesk"
#define MyAppExeName "LLMBatDesk.exe"

[Setup]
AppId={{A30B825E-696D-4CF3-9D80-23BDBD788BD4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\LLMBatDesk
DefaultGroupName=LLMBatDesk
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=LLMBatDesk-Setup
SetupIconFile=..\assets\LLMBatDesk.ico
UninstallDisplayIcon={app}\LLMBatDesk.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "..\dist\LLMBatDesk\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\LLMBatDesk"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\LLMBatDesk"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 LLMBatDesk"; Flags: nowait postinstall skipifsilent

; 用户数据位于 %LOCALAPPDATA%\LLMBatDesk，安装与普通卸载都不会删除该目录。

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if MsgBox(
      '是否同时删除用户配置、数据库和日志？' + #13#10 + #13#10 +
      '选择“否”会保留 %LOCALAPPDATA%\LLMBatDesk，便于以后升级或重新安装。',
      mbConfirmation, MB_YESNO
    ) = IDYES then
      DelTree(ExpandConstant('{localappdata}\LLMBatDesk'), True, True, True);
  end;
end;
