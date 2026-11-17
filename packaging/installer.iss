; Windows installer for Jobhunt (Inno Setup 6).
; Build after PyInstaller:  ISCC.exe /DAppVersion=0.1.0 packaging\installer.iss
; Running a newer installer over an existing install upgrades it in place: the AppId below
; identifies the app to Windows and must never change. User data lives in %LOCALAPPDATA%\Jobhunt
; and is never touched by install, upgrade or uninstall.

#define AppName "Jobhunt"
#define AppExe "Jobhunt.exe"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{3BE387DD-422F-4D9D-BB2B-D6EAA84C99E2}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Khalyl Konkobo
VersionInfoVersion={#AppVersion}
; Per-user by default, so no administrator password is needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist
OutputBaseFilename=Jobhunt-windows-x64-setup
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=auto
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Files of the previous version that the new one no longer ships must not linger.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Jobhunt\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
