; ---------------------------------------------------------------------------
;  SVGPaw — Inno Setup installer script
;
;  Built by build.py after Nuitka has produced dist\SVGPaw\. To build it by
;  hand, open this file in the Inno Setup IDE, or run:
;
;      ISCC.exe installer\svgpaw.iss
;
;  The result lands in dist\SVGPaw-<version>-Setup.exe.
; ---------------------------------------------------------------------------

#define AppName        "SVGPaw"
#define AppSlogan      "Minimize your SVG footprint"
#define AppPublisher   "Tobse"
#define AppExeName     "SVGPaw.exe"
#define AppId          "{{9C2F1B4E-6A7D-4E3C-9B15-2F8D4A6C7E10}"

; build.py passes /DAppVersion=...; this is the fallback for a manual run.
#ifndef AppVersion
  #define AppVersion   "1.0.0"
#endif

#define SourceDir      "..\dist\SVGPaw"
#define IconFile       "..\icon\icon.ico"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoDescription={#AppName} — {#AppSlogan}
VersionInfoVersion={#AppVersion}

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
AllowNoIcons=yes

; Per-user by default, so no UAC prompt; the user can still elevate to install
; for everyone from the installer's own first page.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

OutputDir=..\dist
OutputBaseFilename={#AppName}-{#AppVersion}-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}

; LZMA2/max is worth the extra build minute here: the payload is mostly the
; Tcl/Tk runtime and Python extension modules, which compress very well.
Compression=lzma2/max
SolidCompression=yes
InternalCompressLevel=max

WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german";  MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"
Name: "associate";   Description: "Open .svg and .svgz files with {#AppName}"; \
    GroupDescription: "File associations:"; Flags: unchecked
Name: "contextmenu"; Description: "Add ""Optimize with {#AppName}"" to the right-click menu for SVG files"; \
    GroupDescription: "File associations:"

[Files]
Source: "{#SourceDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*";             DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\sample\*.svg";            DestDir: "{app}\sample"; Flags: ignoreversion
Source: "..\README.md";               DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";                  Filename: "{app}\{#AppExeName}"; \
    Comment: "{#AppSlogan}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";            Filename: "{app}\{#AppExeName}"; \
    Comment: "{#AppSlogan}"; Tasks: desktopicon

[Registry]
; --- our own ProgID; always registered so the context menu and "Open with"
;     have something to point at, even when the user declines the association.
Root: HKA; Subkey: "Software\Classes\{#AppName}.svg"; \
    ValueType: string; ValueName: ""; ValueData: "SVG image"; \
    Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\{#AppName}.svg\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKA; Subkey: "Software\Classes\{#AppName}.svg\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

; --- "Optimize with SVGPaw" on .svg / .svgz, without taking the default over.
Root: HKA; Subkey: "Software\Classes\.svg\shell\{#AppName}"; \
    ValueType: string; ValueName: ""; ValueData: "Optimize with {#AppName}"; \
    Tasks: contextmenu; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.svg\shell\{#AppName}"; \
    ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExeName},0"; \
    Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\.svg\shell\{#AppName}\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\.svgz\shell\{#AppName}"; \
    ValueType: string; ValueName: ""; ValueData: "Optimize with {#AppName}"; \
    Tasks: contextmenu; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\.svgz\shell\{#AppName}\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Tasks: contextmenu

; --- make SVGPaw the default handler, only if that task was ticked.
Root: HKA; Subkey: "Software\Classes\.svg"; \
    ValueType: string; ValueName: ""; ValueData: "{#AppName}.svg"; \
    Tasks: associate; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.svgz"; \
    ValueType: string; ValueName: ""; ValueData: "{#AppName}.svg"; \
    Tasks: associate; Flags: uninsdeletevalue

; --- so SVGPaw shows up under "Open with" regardless.
Root: HKA; Subkey: "Software\Classes\.svg\OpenWithProgids"; \
    ValueType: string; ValueName: "{#AppName}.svg"; ValueData: ""; \
    Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\Applications\{#AppExeName}\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""; \
    Flags: uninsdeletekey

[Run]
Filename: "{app}\{#AppExeName}"; \
    Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Nuitka's onefile cache and our own Python bytecode, if either was created.
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// The user's settings live in %USERPROFILE%\.svgpaw and are deliberately left
// behind on uninstall — unless they ask for a clean sweep.
//
// UninstallSilent() has to be checked explicitly: /SUPPRESSMSGBOXES does not
// reach MsgBox calls made from [Code], so without this guard an unattended
// removal (winget, a deployment script) would hang on a dialog nobody sees.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ConfigDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if UninstallSilent() then
      Exit;
    ConfigDir := ExpandConstant('{%USERPROFILE}\.svgpaw');
    if DirExists(ConfigDir) then
      if MsgBox('Also remove your SVGPaw settings and plugin selection?' + #13#10 +
                ConfigDir, mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(ConfigDir, True, True, True);
  end;
end;
