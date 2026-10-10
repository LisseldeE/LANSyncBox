#ifndef Edition
  #define Edition "standard"
#endif

#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "."
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

#if Edition == "pro"
  #define AppName "LANSyncBox Pro"
  #define ExeName "LANSyncBox Pro.exe"
  #define AppId "{{EE2BE98A-9E6F-4B94-A0EC-5646B8700116}"
  #define OutputBaseFilename "LANSyncBoxPro_Setup"
#else
  #define AppName "LANSyncBox"
  #define ExeName "LANSyncBox.exe"
  #define AppId "{{B7F3A1C2-4D5E-4F60-8A91-2C3D4E5F6A70}"
  #define OutputBaseFilename "LANSyncBox_Setup"
#endif

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
Source: "{#SourceDir}\{#ExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent