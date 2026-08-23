; Phase 6 ONLY — isolated test installer (unique AppId).
; Do NOT use for production releases. Protects %LOCALAPPDATA%\AICA (production AppId).
#define MyAppName "AICA Phase6 Test"
#ifndef MyAppVersion
  #define MyAppVersion "1.0.3"
#endif
#define MyAppPublisher "AICA"
#define MyAppURL "https://github.com/samrudhht2005-dev/aica-project"
#define MyAppExeName "AICA.exe"

[Setup]
; Unique AppId — must never match production {A1CA1000-2026-4A1C-9F10-AICASETUP1000}
AppId={{A1CA6000-PHASE-6TEST-9F10-AICAUPDATE6}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\AICA_Phase6Test\AICA
UsePreviousAppDir=no
DefaultGroupName=AICA Phase6 Test
DisableProgramGroupPage=yes
DisableDirPage=yes
OutputDir=..\..\dist
OutputBaseFilename=AICA_Setup_{#MyAppVersion}_phase6
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
; No desktop icon for test builds
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\dist\AICA.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Updater.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Engine\AICA.Engine.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\dist\AICA.Engine\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\config\config.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\config\README_CONFIG.txt"; DestDir: "{app}"; Flags: ignoreversion
; Phase 6: package a dedicated 1.0.3 version.json without mutating desktop/config/version.json
Source: "..\..\dist\phase6_packaged_version.json"; DestName: "version.json"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDataAica, LocalAica: String;
begin
  if CurStep = ssPostInstall then
  begin
    AppDataAica := ExpandConstant('{localappdata}\AICA_Phase6TestAppData');
    ForceDirectories(AppDataAica);
    ForceDirectories(AppDataAica + '\logs');
    LocalAica := ExpandConstant('{app}');
    ForceDirectories(LocalAica + '\webview');
  end;
end;
