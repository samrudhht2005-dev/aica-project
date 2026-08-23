; Phase 7 ONLY — isolated staging installer (unique AppId).
; Do NOT use for production releases. Protects %LOCALAPPDATA%\AICA (production AppId).
#define MyAppName "AICA Phase7 Test"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.3"
#endif
#define MyAppPublisher "AICA"
#define MyAppURL "https://github.com/samrudhht2005-dev/aica-project"
#define MyAppExeName "AICA.exe"

[Setup]
; Unique AppId — must never match production {A1CA1000-2026-4A1C-9F10-AICASETUP1000}
AppId={{A1CA7000-PHASE-7TEST-9F10-AICAUPDATE7}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\AICA_Phase7Test\AICA
UsePreviousAppDir=no
DefaultGroupName=AICA Phase7 Test
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\..\dist
OutputBaseFilename=AICA_Setup_{#MyAppVersion}_phase7
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
CreateUninstallRegKey=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\dist\AICA.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Updater.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Engine\AICA.Engine.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Engine\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\config\config.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config\README_CONFIG.txt"; DestDir: "{app}"; Flags: ignoreversion
; Packaged version.json supplied separately for staging tests
Source: "..\..\dist\phase7_packaged_version.json"; DestName: "version.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDataAica, LocalAica: String;
begin
  if CurStep = ssPostInstall then
  begin
    AppDataAica := ExpandConstant('{localappdata}\AICA_Phase7TestAppData');
    ForceDirectories(AppDataAica);
    ForceDirectories(AppDataAica + '\logs');
    LocalAica := ExpandConstant('{app}');
    ForceDirectories(LocalAica + '\webview');
  end;
end;
