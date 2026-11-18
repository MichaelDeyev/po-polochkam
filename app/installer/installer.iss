; Установщик программы «По полочкам» (Inno Setup 6). Его собирает app\build.py, передавая значения через /D.
; Ставит программу только для текущего пользователя в %LOCALAPPDATA%\Programs: права администратора не нужны.

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef AppVerText
  #define AppVerText "версия из исходников"
#endif
#define AppName "По полочкам"
#define AppExe "По полочкам.exe"

[Setup]
; AppId не менять: по нему новая версия ставится поверх старой, а не рядом
AppId={{27AB6785-8D7B-48B6-9908-FD6E9C12D8E8}
AppName={#AppName}
AppVersion={#AppVer}
AppVerName={#AppName}, {#AppVerText}
VersionInfoVersion={#AppVer}
VersionInfoDescription={#AppName} — установка
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableWelcomePage=no
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Программа создаёт этот мьютекс, пока работает: установщик и удаление попросят её закрыть
AppMutex=PoPolochkam.Running
CloseApplications=no
SetupIconFile={#IconFile}
WizardStyle=modern
; картинки рисует app\make_icon.py; Inno Setup выбирает по масштабу экрана
WizardImageFile=wizard-164.bmp,wizard-246.bmp,wizard-328.bmp
WizardSmallImageFile=wizard-small-55.bmp,wizard-small-83.bmp,wizard-small-110.bmp
ShowLanguageDialog=no
OutputDir={#OutDir}
OutputBaseFilename={#OutName}
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Messages]
ru.WelcomeLabel1=Установка программы «По полочкам»
ru.SelectTasksDesc=Нужен ли ярлык на рабочем столе?
ru.SelectTasksLabel2=Отметьте, что сделать при установке, и нажмите «Далее».
ru.WizardSelectTasks=Ярлыки
ru.ReadyLabel1=Осталось нажать «Установить».
ru.ReadyLabel2a=Программа установится только для вас, права администратора не нужны. Чтобы что-то поменять, нажмите «Назад».
ru.FinishedHeadingLabel=Программа «По полочкам» установлена
ru.FinishedLabel=Открыть её можно ярлыком «По полочкам» на рабочем столе или в меню «Пуск».
ru.FinishedLabelNoIcons=Открыть её можно из меню «Пуск».
ru.WelcomeLabel2=Программа разложит фото и видео по годам и месяцам, найдёт копии и поможет выбрать лучшие кадры из похожих.%n%nОна работает на этом компьютере, без интернета, и ничего не удаляет сама.

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[InstallDelete]
; файлы прошлой версии: новая кладёт свои целиком
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Guide}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Как пользоваться"; Filename: "{app}\Как пользоваться.txt"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Запустить «{#AppName}»"; Flags: nowait postinstall skipifsilent

[Code]
{ После удаления стираем данные программы: недавние папки, кэш миниатюр (до 2 ГБ), профиль окна, журнал ошибок.
  Журналов отката там нет: они лежат на дисках с фото в папках «_Разбор фото» и остаются на месте.
  Путь берётся из переменной LOCALAPPDATA, как и в самой программе: так тест может подставить свою папку. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Base: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Base := GetEnv('LOCALAPPDATA');
    if Base <> '' then
      DelTree(AddBackslash(Base) + 'РазборФото', True, True, True);
  end;
end;
