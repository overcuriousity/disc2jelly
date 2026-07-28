; Inno Setup script for Disc2Jelly.
;
; Wraps the PyInstaller onedir output into one setup.exe, and collects the
; destination settings during install so the user never opens Settings.
;
; The WebDAV URL and username arrive pre-filled from the baked defaults (they
; are not secret). The password is typed here and written to the per-user
; install_defaults.json on this machine — it is deliberately not compiled into
; the binary. config.py layers that file underneath the user's own config.json,
; so re-running setup never overwrites settings changed in the app.
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
; "local" or "webdav" — preselects the destination page.
#ifndef DefaultDestinationKind
  #define DefaultDestinationKind "local"
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
  if '{#DefaultDestinationKind}' = 'webdav' then
    DestPage.Values[1] := True
  else
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

{ Escape for JSON, and force the result to pure ASCII.

  SaveStringToFile writes the system ANSI codepage while config.py reads
  UTF-8, so an umlaut in a path, user name or password used to produce a
  file Python could not decode -- and config.load swallows the resulting
  ValueError, so the whole configuration vanished with no error at all.
  Emitting every non-ASCII character as \uXXXX sidesteps the codepage
  entirely; each UTF-16 code unit maps to one escape, which is valid JSON
  including for surrogate pairs. Preferred over SaveStringToUTF8File,
  which prepends a BOM that json.loads rejects. }
function JsonEscape(const S: String): String;
var
  I, Code: Integer;
  Ch: Char;
begin
  Result := '';
  for I := 1 to Length(S) do
  begin
    Ch := S[I];
    Code := Ord(Ch);
    if Ch = '\' then Result := Result + '\\'
    else if Ch = '"' then Result := Result + '\"'
    else if (Code < 32) or (Code > 126) then
      Result := Result + Format('\u%.4x', [Code])
    else Result := Result + Ch;
  end;
end;

{ Write the installer's choices to install_defaults.json, NOT config.json.

  config.py layers this file underneath the user's own config.json, so
  reinstalling or upgrading can never destroy settings the user changed
  in the app. }
procedure WriteConfig;
var
  ConfigDir, DefaultsFile, Json: String;
begin
  ConfigDir := ExpandConstant('{userappdata}\disc2jelly');
  ForceDirectories(ConfigDir);
  DefaultsFile := ConfigDir + '\install_defaults.json';

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

  SaveStringToFile(DefaultsFile, Json, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteConfig;
end;
