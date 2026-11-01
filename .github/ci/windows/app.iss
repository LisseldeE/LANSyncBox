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
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; 目录版：整个 standalone 目录递归安装（含 PySide6\qt-plugins 等全部依赖）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent