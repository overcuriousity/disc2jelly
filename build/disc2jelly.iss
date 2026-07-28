; Inno Setup script for Disc2Jelly.
;
; Wraps the PyInstaller onedir output into one setup.exe, and collects the
; destination settings during install so the user never opens Settings.
;
; Every field is pre-filled from build_config.toml via the generated
; wizard_defaults.isi, and each page is skipped once nothing on it is still
; unanswered — set webdav_url, webdav_user and webdav_password there and the
; installer asks the end user nothing at all. Leave webdav_password empty and
; the wizard collects it on the target machine instead; that is the safer
; default, because anything in wizard_defaults.isi is compiled into Setup.exe
; and recoverable with `strings`.
;
; The answers are written to the per-user install_defaults.json. config.py
; layers that file underneath the user's own config.json, so re-running setup
; never overwrites settings changed in the app.
;
; Build:  iscc build\disc2jelly.iss

#define AppName "Disc2Jelly"
#define AppVersion "2.0.0"
#define AppPublisher "Disc2Jelly"
#define AppExe "Disc2Jelly.exe"

; Wizard defaults, generated from build_config.toml by
; build/gen_wizard_defaults.py (run by build_windows.ps1). Absent for a build
; with no build_config.toml, in which case the wizard asks for everything.
#if FileExists(AddBackslash(SourcePath) + "wizard_defaults.isi")
  #include "wizard_defaults.isi"
#endif
#ifndef DefaultWebdavUrl
  #define DefaultWebdavUrl ""
#endif
#ifndef DefaultWebdavUser
  #define DefaultWebdavUser ""
#endif
#ifndef DefaultWebdavPassword
  #define DefaultWebdavPassword ""
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
  WebdavPage.Values[2] := '{#DefaultWebdavPassword}';
end;

function UseWebdav: Boolean;
begin
  Result := DestPage.Values[1];
end;

function CredentialsAreComplete: Boolean;
begin
  Result := ('{#DefaultWebdavUrl}' <> '') and ('{#DefaultWebdavUser}' <> '')
            and ('{#DefaultWebdavPassword}' <> '');
end;

{ build_config.toml already answers every destination question. }
function FullyConfigured: Boolean;
begin
  if '{#DefaultDestinationKind}' = 'webdav' then
    Result := CredentialsAreComplete
  else
    Result := '{#DefaultLocalPath}' <> '';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = DestPage.ID then Result := FullyConfigured;
  if PageID = LocalPage.ID then Result := UseWebdav or FullyConfigured;
  { Nothing left to ask when URL, user and password all came from the build. }
  if PageID = WebdavPage.ID then
    Result := (not UseWebdav) or CredentialsAreComplete;
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
{ Empty fields are left out entirely rather than written as "": a blank
  wizard box must not overwrite a value baked in at build time. }
procedure AddPair(var Body: String; const Key, Value: String);
begin
  if Value = '' then
    Exit;
  if Body <> '' then
    Body := Body + ',' + #13#10;
  Body := Body + '  "' + Key + '": "' + JsonEscape(Value) + '"';
end;

procedure WriteConfig;
var
  ConfigDir, DefaultsFile, Body: String;
begin
  ConfigDir := ExpandConstant('{userappdata}\disc2jelly');
  ForceDirectories(ConfigDir);
  DefaultsFile := ConfigDir + '\install_defaults.json';

  Body := '';
  if UseWebdav then
  begin
    AddPair(Body, 'destination_kind', 'webdav');
    AddPair(Body, 'webdav_url', WebdavPage.Values[0]);
    AddPair(Body, 'webdav_user', WebdavPage.Values[1]);
    AddPair(Body, 'webdav_password', WebdavPage.Values[2]);
  end
  else
  begin
    AddPair(Body, 'destination_kind', 'local');
    AddPair(Body, 'local_path', LocalPage.Values[0]);
  end;

  SaveStringToFile(DefaultsFile, '{' + #13#10 + Body + #13#10 + '}' + #13#10, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    WriteConfig;
end;
