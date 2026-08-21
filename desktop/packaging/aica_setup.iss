; AICA Windows installer — Inno Setup 6+
; Installs/upgrades ONE canonical per-user app: %LOCALAPPDATA%\AICA\
; Same AppId across versions so future setups upgrade in place.

#define MyAppName "AICA"
#define MyAppVersion "1.0.1"
#define MyAppPublisher "AICA"
#define MyAppURL "https://github.com/samrudhht2005-dev/aica-project"
#define MyAppExeName "AICA.exe"

[Setup]
AppId={{A1CA1000-2026-4A1C-9F10-AICASETUP1000}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\AICA
UsePreviousAppDir=no
DefaultGroupName=AICA
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\..\dist
OutputBaseFilename=AICA_Setup_{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install under LocalAppData (no admin required)
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; Launcher
Source: "..\..\dist\AICA.exe"; DestDir: "{app}"; Flags: ignoreversion
; Engine onedir flattened beside launcher (AICA.Engine.exe + _internal)
Source: "..\..\dist\AICA.Engine\AICA.Engine.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Engine\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
; Config template (user data lives in %AppData%\AICA\config.env)
Source: "..\config\config.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config\README_CONFIG.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config\version.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Always create one official Desktop shortcut
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch AICA"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2RuntimeInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')
    or RegKeyExists(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}')
    or RegKeyExists(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
end;

function InitializeSetup: Boolean;
begin
  Result := True;
  if not WebView2RuntimeInstalled then
  begin
    MsgBox('Microsoft Edge WebView2 Runtime was not detected.'#13#10#13#10
      'AICA needs WebView2 to display the application.'#13#10
      'Install it from Microsoft, then re-run this installer if launch fails:'#13#10
      'https://developer.microsoft.com/microsoft-edge/webview2/',
      mbInformation, MB_OK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDataAica, LocalAica, Cfg: String;
begin
  if CurStep = ssPostInstall then
  begin
    { Roaming config/logs (existing desktop convention) }
    AppDataAica := ExpandConstant('{userappdata}\AICA');
    ForceDirectories(AppDataAica);
    ForceDirectories(AppDataAica + '\logs');
    Cfg := AppDataAica + '\config.env';
    if not FileExists(Cfg) then
      FileCopy(ExpandConstant('{app}\config.env.example'), Cfg, False);

    { Local user data: WebView2 profile + future desktop settings }
    LocalAica := ExpandConstant('{localappdata}\AICA');
    ForceDirectories(LocalAica);
    ForceDirectories(LocalAica + '\webview');
    ForceDirectories(LocalAica + '\logs');
  end;
end;
