; Inno Setup script for Disc2Jelly.
;
; Wraps the PyInstaller onedir output into one setup.exe, and collects the
; destination settings during install so the user never opens Settings.
;
; The WebDAV URL and username arrive pre-filled from the baked defaults (they
; are not secret). The password is typed here and written to the per-user
; config on this machine — it is deliberately not compiled into the binary.
;
; Build:  iscc build\disc2jelly.iss

#define AppName "Disc2Jelly"
#define AppVersion "2.0.0"
#define AppPublisher "Disc2Jelly"
#define AppExe "Disc2Jelly.exe"

; Overridden by build_windows.ps1 via /D switches.
#ifndef DefaultWebdavUrl
  #define DefaultWebdavUrl ""
#endif
#ifndef DefaultWebdavUser
  #define DefaultWebdavUser ""
#endif
#ifndef DefaultLocalPath
  #define DefaultLocalPath ""
#endif

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=..\dist
OutputBaseFilename=Disc2Jelly-Setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\Disc2Jelly\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
var
  DestPage: TInputOptionWizardPage;
  LocalPage: TInputDirWizardPage;
  WebdavPage: TInputQueryWizardPage;

procedure InitializeWizard;
begin
  DestPage := CreateInputOptionPage(wpSelectTasks,
    'Where should finished films go?',
    'Disc2Jelly saves each film into a Jellyfin-ready folder.',
    'Choose one:', True, False);
  DestPage.Add('A folder on this PC or the network (simplest)');
  DestPage.Add('A WebDAV server');
  DestPage.Values[0] := True;

  LocalPage := CreateInputDirPage(DestPage.ID,
    'Destination folder', 'Where should Disc2Jelly save films?',
    'Point this at the same folder your Jellyfin movie library uses.',
    False, '');
  LocalPage.Add('');
  if '{#DefaultLocalPath}' <> '' then
    LocalPage.Values[0] := '{#DefaultLocalPath}'
  else
    LocalPage.Values[0] := ExpandConstant('{userdocs}\..\Videos\Disc2Jelly');

  WebdavPage := CreateInputQueryPage(LocalPage.ID,
    'WebDAV server', 'Where should Disc2Jelly upload films?',
    'The password is stored on this PC only. Use an app password scoped to ' +
    'this share, not your main account password.');
  WebdavPage.Add('Server address:', False);
  WebdavPage.Add('User name:', False);
  WebdavPage.Add('Password:', True);
  WebdavPage.Values[0] := '{#DefaultWebdavUrl}';
  WebdavPage.Values[1] := '{#DefaultWebdavUser}';
end;

function UseWebdav: Boolean;
begin
  Result := DestPage.Values[1];
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = LocalPage.ID then Result := UseWebdav;
  if PageID = WebdavPage.ID then Result := not UseWebdav;
end;

function JsonEscape(const S: String): String;
var
  I: Integer;
  Ch: Char;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    Ch := S[I];
    if Ch = '\' then Result := Result + '\\'
    else if Ch = '"' then Result := Result + '\"'
    else Result := Result + Ch;
  end;
end;

procedure WriteConfig;
var
  ConfigDir, ConfigFile, Json: String;
begin
  ConfigDir := ExpandConstant('{userappdata}\disc2jelly');
  ForceDirectories(ConfigDir);
  ConfigFile := ConfigDir + '\config.json';

  if UseWebdav then
    Json :=
      '{' + #13#10 +
      '  "destination_kind": "webdav",' + #13#10 +
      '  "webdav_url": "' + JsonEscape(WebdavPage.Values[0]) + '",' + #13#10 +
      '  "webdav_user": "' + JsonEscape(WebdavPage.Values[1]) + '",' + #13#10 +
      '  "webdav_password": "' + JsonEscape(WebdavPage.Values[2]) + '"' + #13#10 +
      '}' + #13#10
  else
    Json :=
      '{' + #13#10 +
      '  "destination_kind": "local",' + #13#10 +
      '  "local_path": "' + JsonEscape(LocalPage.Values[0]) + '"' + #13#10 +
      '}' + #13#10;

  SaveStringToFile(ConfigFile, Json, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteConfig;
end;
