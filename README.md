# TechLauncher

Лаунчер для учебных ПК на Python: **Minecraft** и **SCP: Containment Breach**.
Работает на Windows и Linux (проверялся под Ubuntu и рассчитан на РЕД ОС),
**без прав администратора** и без сторонних библиотек. Нужен только Python 3.7+.

- **Minecraft**: собственная реализация по образцу [PortableMC](https://github.com/theorzr/portablemc).
  Лаунчер сам скачивает версию, библиотеки, ассеты и Java от Mojang. Можно играть
  с офлайн-ником или войти через Microsoft.
- **SCP: Containment Breach**: скачивание с официального сайта, моды из архивов,
  настройки `options.ini`. На Linux игра запускается через переносной Wine, который
  лаунчер скачивает сам (sudo не нужен).

## Самый простой способ: запускалка с автообновлением

Нужен один файл, [start.py](start.py). Он сам скачивает свежую версию лаунчера
с GitHub и запускает её. Без интернета запускает уже скачанную версию.

```sh
curl -O https://raw.githubusercontent.com/AxxRipz2016/techlauncher/main/start.py
python3 start.py            # окно
python3 start.py doctor     # или любые команды лаунчера
```

Если `curl` нет, откройте ссылку в браузере и сохраните файл (Ctrl+S).
Лаунчер хранится в `<папка лаунчера>/app/current`. `--no-update` пропускает проверку обновлений.

## Запуск из папки проекта

```sh
python3 -m techlauncher          # окно (если в Python есть tkinter)
python3 -m techlauncher --help   # команды в терминале
python3 -m techlauncher doctor   # проверить компьютер
```

На Windows вместо `python3` используйте `py`.

## Команды

| Команда | Что делает |
|---|---|
| `doctor` | проверка: ОС, Python, tkinter, права, Wine, Flatpak, доступ к сайтам |
| `mc versions [--snapshots]` | список версий Minecraft |
| `mc install [ВЕРСИЯ]` | скачать версию (по умолчанию последний релиз) |
| `mc start [ВЕРСИЯ] -u НИК` | запустить с офлайн-ником (недостающее докачается) |
| `mc start -a ИМЯ` | запустить с аккаунтом Microsoft |
| `mc auth login / list / logout ИМЯ` | вход через Microsoft, список, выход |
| `scp install` | скачать SCP:CB |
| `scp import АРХИВ --name ИМЯ` | добавить мод |
| `scp start [--resolution 1280x720] [--fullscreen]` | запустить SCP:CB |
| `scp config [ключ=значение]` | показать или изменить `options.ini` |
| `wine install` | (Linux) скачать переносной Wine |

Опции `mc start`: `--ram 2048`, `--resolution 1280x720`, `--server host:port`,
`--java ПУТЬ`, `--jvm-arg=-X...`, `--dry` (показать команду, не запуская).

## Где лежат файлы

```
Windows: %APPDATA%\techlauncher      Linux: ~/.local/share/techlauncher
  minecraft/game/        сохранения, моды, настройки Minecraft
  minecraft/versions/, libraries/, assets/
  java/                  Java от Mojang
  scpcb/instances/       копии SCP:CB (сохранения в Saves/)
  wine/, wine-prefix/    переносной Wine (Linux)
  settings.json          запомненные ник, версия, память
  accounts.json          токены Microsoft (только для владельца)
```

Другая папка задаётся через `--main-dir ПУТЬ` или переменную `TECHL_MAIN_DIR`,
например, чтобы держать всё на флешке.

## Вход через Microsoft

Mojang пускает к Minecraft API только **одобренные приложения**. Поэтому для входа
нужен свой Azure Client ID:

1. Зайдите на https://portal.azure.com → **App registrations** → **New registration**.
   Тип аккаунтов: *Personal Microsoft accounts only*. Redirect URI не нужен.
2. В разделе **Authentication** включите **Allow public client flows**.
3. Скопируйте **Application (client) ID**.
4. Подайте заявку на доступ к Minecraft API: https://aka.ms/mce-reviewappid.
   Пока Mojang не одобрит приложение, вход будет заканчиваться ошибкой 403.
5. `python3 -m techlauncher mc auth login --client-id ВАШ_ID` (ID запомнится).

На общем компьютере после игры выходите из аккаунта: `mc auth logout ИМЯ`
или кнопка «Выйти из аккаунта» в окне.

## Известные особенности

- **SCP:CB под Wine падала на 40% загрузки** с ошибкой «Memory access violation».
  Причин оказалось две, и лаунчер исправляет обе сам:
  1. Старый движок Blitz3D видел отрицательный объём видеопамяти. Лаунчер прописывает
     Wine 512 МБ (`scp start --vram`).
  2. Вступительное видео: под Wine нет видеокодеков, и `BlitzMovie.dll` портит память.
     На Linux видео выключается (`play startup video = false`); вернуть можно
     флагом `scp start --startup-video`.
- **Окно без tkinter.** tkinter нельзя поставить через pip: это часть самого Python,
  а в Linux её выносят в системный пакет `python3-tkinter`, который ставится только
  с sudo. Поэтому `start.py`, если окна нет, сам скачивает переносной Python со
  встроенным tkinter ([python-build-standalone](https://github.com/astral-sh/python-build-standalone),
  ~33 МБ, проверка SHA-256) в `<папка лаунчера>/python` и перезапускается через него.
  Команды в терминале работают и без этого.

## Как это устроено

| Модуль | Назначение |
|---|---|
| `net.py` | загрузка в 16 потоков с keep-alive, докачкой и проверкой SHA-1/SHA-256 |
| `mc/versions.py` | манифест Mojang, JSON версий, наследование `inheritsFrom` |
| `mc/rules.py` | правила ОС/архитектуры/features из JSON версий |
| `mc/install.py` | библиотеки, нативные файлы, ассеты, client.jar, конфиг логов |
| `mc/java.py` | Java от Mojang |
| `mc/auth.py` | офлайн-UUID как у игры, вход Microsoft → Xbox → Minecraft |
| `mc/launch.py` | подстановка `${...}` в аргументы и запуск |
| `scpcb.py`, `wine.py`, `ini.py` | SCP:CB, Wine, правка `options.ini` |
| `doctor.py`, `cli.py`, `gui.py` | проверка ПК, команды, окно |

Тесты: `python3 -m unittest discover -s tests`

## Лицензии игр

Minecraft — собственность Mojang/Microsoft; лаунчер скачивает файлы с официальных
серверов Mojang. SCP: Containment Breach — CC BY-SA 3.0 (Undertow Games), скачивается
с официального сайта.
