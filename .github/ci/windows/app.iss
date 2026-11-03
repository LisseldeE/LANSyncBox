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
; 升级时不沿用旧版安装目录：旧版被卸载后装到上面的 DefaultDirName（路径中不再含 Pro）
UsePreviousAppDir=no
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
  UninstallParams = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';
  LegacyUninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1';

{ 旧版（原 LANSyncBox Pro）的安装目录与新版不同，而 Inno Setup 只会复用/覆盖，
  不会自动卸载旧版本。这里在复制文件前静默运行旧版卸载程序，避免两个版本共存。 }
procedure RunUninstaller(const Uninstaller: string);
var
  ResultCode: Integer;
begin
  if FileExists(Uninstaller) then
    Exec(Uninstaller, UninstallParams, '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure UninstallLegacyVersion;
var
  InstallLocation, UninstallString: string;
begin
  { 旧版默认目录（32 位与 64 位安装模式都覆盖） }
  RunUninstaller(ExpandConstant('{commonpf32}\LANSyncBox Pro\unins000.exe'));
  RunUninstaller(ExpandConstant('{commonpf64}\LANSyncBox Pro\unins000.exe'));

  { 兼容用户自定义过安装目录的旧版：按同一 AppId 的卸载项查找，排除当前安装目录 }
  if RegQueryStringValue(HKLM32, LegacyUninstallKey, 'InstallLocation', InstallLocation)
     and (LowerCase(InstallLocation) <> LowerCase(ExpandConstant('{app}')))
     and RegQueryStringValue(HKLM32, LegacyUninstallKey, 'UninstallString', UninstallString) then
    RunUninstaller(RemoveQuotes(UninstallString));
  if RegQueryStringValue(HKLM64, LegacyUninstallKey, 'InstallLocation', InstallLocation)
     and (LowerCase(InstallLocation) <> LowerCase(ExpandConstant('{app}')))
     and RegQueryStringValue(HKLM64, LegacyUninstallKey, 'UninstallString', UninstallString) then
    RunUninstaller(RemoveQuotes(UninstallString));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    UninstallLegacyVersion;
end;