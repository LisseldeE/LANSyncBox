#ifndef AppVersion
  #define AppVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "."
#endif
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

#define AppName "LANSyncBox"
#define ExeName "LANSyncBox.exe"
#define AppId "{{EE2BE98A-9E6F-4B94-A0EC-5646B8700116}"
#define AppIdKey "{EE2BE98A-9E6F-4B94-A0EC-5646B8700116}_is1"
#define StandardAppIdKey "{AFCA0D26-F6B7-442C-9A77-C11AD8971EC2}_is1"
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
UsePreviousAppDir=yes
UsePreviousTasks=no
UsePreviousGroup=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
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
english.OldUninstallFailed=Failed to remove the old version. Setup stopped to avoid mixing old and new files. Please close any running instance of the app and run this installer again.
chinesesimp.OldUninstallFailed=卸载旧版本失败，安装已中止以避免新旧文件混用。请关闭正在运行的程序后重新运行本安装程序。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
Type: files; Name: "{commondesktop}\LANSyncBox Pro.lnk"
Type: files; Name: "{commonprograms}\LANSyncBox Pro\LANSyncBox Pro.lnk"
Type: dirifempty; Name: "{commonprograms}\LANSyncBox Pro"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#ExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#ExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#ExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall

[Code]
const
  LegacyDirName = 'LANSyncBox Pro';
  LegacyExeName = 'LANSyncBox Pro.exe';
  TargetDirName = 'LANSyncBox';
  UninstallParams = '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART';

var
  PreviousDir: String;

function NormalizedTarget(const Path: String): String;
begin
  Result := Path;
  if CompareText(ExtractFileName(Path), LegacyDirName) = 0 then
    Result := ExtractFilePath(Path) + TargetDirName;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectDir then
  begin
    if PreviousDir = '' then
      PreviousDir := RemoveBackslash(WizardForm.DirEdit.Text);
    WizardForm.DirEdit.Text := NormalizedTarget(WizardForm.DirEdit.Text);
  end;
end;

function QueryUninstaller(const RootKey: Integer; const SubKey: String): String;
var
  Value: String;
  P: Integer;
begin
  Result := '';
  if not RegQueryStringValue(RootKey, SubKey, 'UninstallString', Value) then Exit;
  Value := Trim(Value);
  if (Length(Value) > 1) and (Value[1] = '"') then
  begin
    P := Pos('"', Copy(Value, 2, Length(Value) - 1));
    if P > 0 then Value := Copy(Value, 2, P - 1);
  end
  else
  begin
    P := Pos(' ', Value);
    if P > 0 then Value := Copy(Value, 1, P - 1);
  end;
  Result := Value;
end;

function FindUninstallerInDir(const Dir: String): String;
var
  Candidate: String;
  I: Integer;
begin
  Result := '';
  for I := 0 to 99 do
  begin
    Candidate := Dir + '\unins' + Format('%.3d', [I]) + '.exe';
    if FileExists(Candidate) then Result := Candidate;
  end;
end;

function QueryAppUninstaller(const AppKey: String): String;
var
  Key: String;
begin
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' + AppKey;
  Result := QueryUninstaller(HKLM, Key);
  if (Result = '') or not FileExists(Result) then
    Result := QueryUninstaller(HKLM32, Key);
end;

function FindUninstaller(const OldDir: String): String;
begin
  Result := QueryAppUninstaller('{#AppIdKey}');
  if (Result = '') or not FileExists(Result) then
    Result := FindUninstallerInDir(OldDir);
end;

function RemoveInstallation(const Dir, Uninstaller: String): Boolean;
var
  ResultCode, WaitCount: Integer;
begin
  Result := False;
  if not Exec(Uninstaller, UninstallParams, '', SW_HIDE, ewWaitUntilTerminated,
              ResultCode) then Exit;
  if ResultCode <> 0 then Exit;
  WaitCount := 0;
  while FileExists(Dir + '\{#ExeName}') or
        FileExists(Dir + '\' + LegacyExeName) do
  begin
    Sleep(250);
    WaitCount := WaitCount + 1;
    if WaitCount >= 40 then Exit;
  end;
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OldDir, Uninstaller: String;
  ResultCode: Integer;
begin
  Result := '';
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#ExeName}', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM ' + LegacyExeName, '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if PreviousDir <> '' then
    OldDir := PreviousDir
  else
    OldDir := RemoveBackslash(ExpandConstant('{app}'));
  if ExtractFileName(OldDir) = '' then Exit;

  Uninstaller := FindUninstaller(OldDir);
  if Uninstaller <> '' then
  begin
    if FileExists(ExtractFilePath(Uninstaller) + '{#ExeName}') or
       FileExists(ExtractFilePath(Uninstaller) + LegacyExeName) then
    begin
      if not RemoveInstallation(RemoveBackslash(ExtractFilePath(Uninstaller)),
                                Uninstaller) then
      begin
        Result := ExpandConstant('{cm:OldUninstallFailed}');
        Exit;
      end;
    end;
  end;

  Uninstaller := QueryAppUninstaller('{#StandardAppIdKey}');
  if Uninstaller <> '' then
  begin
    if FileExists(ExtractFilePath(Uninstaller) + '{#ExeName}') or
       FileExists(ExtractFilePath(Uninstaller) + LegacyExeName) then
    begin
      if not RemoveInstallation(RemoveBackslash(ExtractFilePath(Uninstaller)),
                                Uninstaller) then
      begin
        Result := ExpandConstant('{cm:OldUninstallFailed}');
        Exit;
      end;
    end;
  end;
end;
