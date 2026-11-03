#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "."
#endif
; 打包来源目录：Nuitka --standalone 的 .dist 目录（目录版），默认回落到 SourceDir
#ifndef DistDir
  #define DistDir SourceDir
#endif
#ifndef OutputDir
  #define OutputDir SourceDir
#endif
#ifndef SetupIcon
  #define SetupIcon SourceDir + "\icon.ico"
#endif
#ifndef LicenseFile
  #define LicenseFile SourceDir + "\LICENSE.txt"
#endif

; AppId 沿用历史 Pro 版的 GUID，保证已安装旧版本可被本安装包覆盖升级
#define AppName "LANSyncBox"
#define ExeName "LANSyncBox.exe"
#define AppId "{{EE2BE98A-9E6F-4B94-A0EC-5646B8700116}"
#define OutputBaseFilename "LANSyncBox_Setup"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Lisselde_E
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} 安装程序
VersionInfoOriginalFilename={#OutputBaseFilename}.exe
DefaultDirName={autopf}\{#AppName}
; 升级时沿用上次安装目录；若上次目录名是旧版的 "LANSyncBox Pro"，
; 由 [Code] 在安装前把目录整体改名为 "LANSyncBox"，父目录保持不变
UsePreviousAppDir=yes
; 不沿用上次的任务勾选：桌面快捷方式每次安装都默认勾选，避免旧版卸载后不再重建
UsePreviousTasks=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile={#LicenseFile}
UninstallDisplayIcon={app}\{#ExeName}
UninstallDisplayName={#AppName}
PrivilegesRequired=admin
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#SetupIcon}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
english.CreateDesktopIcon=Create a desktop shortcut
english.AdditionalIcons=Additional shortcuts:
english.LaunchProgram=Run %1
chinesesimp.CreateDesktopIcon=创建桌面快捷方式
chinesesimp.AdditionalIcons=附加快捷方式:
chinesesimp.LaunchProgram=运行 %1

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 旧版（LANSyncBox Pro）留下的快捷方式：目录改名带不走它们，需显式清理
Type: files; Name: "{commondesktop}\LANSyncBox Pro.lnk"
Type: files; Name: "{commonprograms}\LANSyncBox Pro\LANSyncBox Pro.lnk"
Type: dirifempty; Name: "{commonprograms}\LANSyncBox Pro"

[Files]
; 目录版：整个 standalone 目录递归安装（含 PySide6\qt-plugins 等全部依赖）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  LegacyDirName = 'LANSyncBox Pro';
  LegacyExeName = 'LANSyncBox Pro.exe';
  TargetDirName = 'LANSyncBox';
  UninstallParams = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';

var
  PreviousDir: String;

{ 只替换路径最后一段目录名。不能全局替换 " Pro"——"Program Files" 里也含 "Pro"。 }
function NormalizedTarget(const Path: String): String;
begin
  Result := Path;
  if CompareText(ExtractFileName(Path), LegacyDirName) = 0 then
    Result := ExtractFilePath(Path) + TargetDirName;
end;

procedure InitializeWizard;
begin
  { UsePreviousAppDir=yes 时这里已是上次安装目录（仅 64 位安装，键在 HKLM64） }
  PreviousDir := RemoveBackslash(WizardForm.DirEdit.Text);
  WizardForm.DirEdit.Text := NormalizedTarget(PreviousDir);
end;

{ 复制文件前静默卸载旧版：由旧卸载器按自己的记录删除全部旧文件，随后在新目录全量
  安装，避免"同目录覆盖"留下旧主程序 / 旧 Qt 插件等与新版混用。 }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OldDir, Uninstaller: String;
  ResultCode: Integer;
begin
  Result := '';
  { 关闭新旧主程序，确保旧卸载器能删除占用中的文件 }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#ExeName}', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + LegacyExeName, '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if PreviousDir = '' then Exit;
  OldDir := RemoveBackslash(PreviousDir);
  { 防止异常路径（空目录名 / 盘符根）被误卸载 }
  if ExtractFileName(OldDir) = '' then Exit;
  Uninstaller := OldDir + '\unins000.exe';
  if not FileExists(Uninstaller) then Exit;

  Exec(Uninstaller, UninstallParams, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;