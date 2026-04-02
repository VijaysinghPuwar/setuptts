; =============================================================================
;  SetupTTS — Inno Setup Installer Script
;  Inno Setup 6 required: https://jrsoftware.org/isdl.php
;
;  CI invocation (from repo root, PowerShell):
;    & "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
;        /DAppVersion=1.5.0 `
;        /DSourceDir=C:\path\to\dist\SetupTTS `
;        /DOutputDir=C:\path\to\installer_out `
;        installers\windows.iss
;
;  Local invocation (from repo root):
;    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installers\windows.iss
;    (uses defaults: AppVersion=1.5.0, SourceDir=..\dist\SetupTTS, OutputDir=out)
;
;  Note: SourceDir must point to the onedir OUTPUT folder (dist\SetupTTS\),
;  not to dist\ itself. The onedir build avoids per-launch self-extraction.
; =============================================================================

; ── Overridable via ISCC /D command-line defines ─────────────────────────────
#ifndef AppVersion
  #define AppVersion "1.5.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\SetupTTS"
#endif

#ifndef OutputDir
  #define OutputDir "out"
#endif

; ── Fixed defines ─────────────────────────────────────────────────────────────
#define AppName      "SetupTTS"
#define AppPublisher "SetupTTS"
#define AppURL       "https://github.com/VijaysinghPuwar/setuptts"
#define AppExe       "SetupTTS.exe"

[Setup]
AppId={{29A84364-234A-48E0-99EA-B69C984270F3}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=SetupTTS-Windows-Installer
SetupIconFile=..\app\assets\icons\app.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

WizardImageFile=compiler:WizModernImage.bmp
WizardSmallImageFile=compiler:WizModernSmallImage.bmp

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; PyInstaller onedir build — copy all files from the dist\SetupTTS\ folder.
; No self-extraction at launch; app starts immediately from installed files.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";           Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";     Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User data (settings, history) is left untouched on uninstall by default.
; Uncomment below to also remove user data on uninstall:
; Type: filesandordirs; Name: "{localappdata}\SetupTTS"
