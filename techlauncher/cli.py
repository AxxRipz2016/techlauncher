"""Команды в терминале. Без аргументов открывается окно (если есть tkinter)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

from . import LauncherError, __version__, doctor, scpcb, wine
from .mc import auth as mc_auth
from .mc import launch as mc_launch
from .mc import versions as mc_versions
from .paths import Layout, Settings, default_main_dir

# ------------------------------------------------------------------ вывод


def fmt_size(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class ProgressBar:
    """Полоска прогресса в терминале: progress(байт, всего_байт, файлов, всего_файлов)."""

    def __init__(self, label: str = "загрузка"):
        self.label = label
        self.start = time.monotonic()
        self.tty = sys.stdout.isatty()
        self.shown = False

    def __call__(self, done: int, total: int, files: int, total_files: int, label: str | None = None) -> None:
        if not self.tty:
            return
        speed = done / max(time.monotonic() - self.start, 1e-6)
        pct = done * 100 // total if total else files * 100 // max(total_files, 1)
        filled = 30 * pct // 100
        text = (f"  {label or self.label} [{'#' * filled}{'-' * (30 - filled)}] {pct:3d}%  "
                f"{fmt_size(done)}/{fmt_size(total)}  файлов {files}/{total_files}  {fmt_size(speed)}/с")
        print("\r" + text.ljust(110), end="", flush=True)
        self.shown = True

    def stage(self, name: str, done: int, total: int, files: int, total_files: int) -> None:
        self(done, total, files, total_files, label=name)
        if files == total_files:
            self.finish()

    def finish(self) -> None:
        if self.shown:
            print()
            self.shown = False


def info(msg: str) -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"внимание: {msg}", file=sys.stderr, flush=True)


def parse_resolution(text: str) -> Tuple[int, int]:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError("формат: ШИРИНАxВЫСОТА, например 1280x720") from None


def parse_kv(text: str) -> Tuple[Optional[str], str, str]:
    if "=" not in text:
        raise argparse.ArgumentTypeError("формат: ключ=значение или секция/ключ=значение")
    k, v = text.split("=", 1)
    section = None
    if "/" in k:
        section, k = k.split("/", 1)
    return (section.strip() if section else None), k.strip(), v.strip()


# ------------------------------------------------------------------ общие


def cmd_doctor(args, layout: Layout) -> int:
    checks = doctor.run(layout)
    width = max(len(c.title) for c in checks)
    for c in checks:
        print(f"[{c.status}] {c.title.ljust(width)}  {c.detail}")
        if c.advice:
            print(f"     {' ' * width}  -> {c.advice}")
    failed = sum(c.status == doctor.FAIL for c in checks)
    warned = sum(c.status == doctor.WARN for c in checks)
    print()
    if failed:
        print(f"Есть проблемы ({failed}), см. подсказки выше.")
        return 1
    print("Есть предупреждения, см. подсказки выше." if warned else "Всё в порядке.")
    return 0


def cmd_gui(args, layout: Layout) -> int:
    from . import gui
    return gui.run(layout)


def cmd_wine(args, layout: Layout) -> int:
    c = args.wine_cmd
    if c != "remove" and not wine.needs_wine():
        info("Windows: Wine не нужен, игры запускаются напрямую")
        return 0
    if c == "install":
        bar = ProgressBar("Wine")
        binary = wine.install(layout, args.tag, Path(args.file).expanduser() if args.file else None,
                              progress=lambda b, t, f, tf: bar(b, t, f, tf), log=info, force=args.force)
        bar.finish()
        info(f"Wine готов: {binary}")
    elif c == "status":
        try:
            rows = wine.status(layout)
        except LauncherError as e:
            info(str(e))
            return 1
        for k, v in rows:
            print(f"{k + ':' if k else '':<22}{v}")
    elif c == "kill":
        info("программы Wine закрыты" if wine.kill(wine.resolve(layout), layout.wine_prefix)
             else "запущенных программ Wine нет")
    elif c == "reset":
        if not args.yes:
            if not sys.stdin.isatty():
                raise LauncherError("для сброса без вопроса используйте -y")
            if input("Удалить папку настроек Wine? Сохранения игр не пострадают. [y/N] "
                     ).strip().lower() not in ("y", "д", "да", "yes"):
                info("отменено")
                return 0
        info("папка настроек удалена, при запуске игры создастся заново"
             if wine.reset_prefix(layout) else "папки настроек ещё нет")
    elif c == "remove":
        info("удалено" if wine.remove(layout) else "переносной Wine не установлен")
    return 0


# ------------------------------------------------------------------ Minecraft


def cmd_mc_versions(args, layout: Layout) -> int:
    installed = set(mc_versions.installed(layout))
    man = mc_versions.manifest(layout)
    if args.installed or man is None:
        if man is None:
            warn("нет связи с Mojang, показаны только установленные версии")
        for v in sorted(installed):
            print(v)
        return 0
    types = {"release"} | ({"snapshot"} if args.snapshots else set()) \
        | ({"old_beta", "old_alpha"} if args.old else set())
    rows = [v for v in man["versions"] if v["type"] in types
            and (not args.filter or any(f in v["id"] for f in args.filter))]
    for v in rows[: args.limit]:
        mark = "  [установлена]" if v["id"] in installed else ""
        print(f"{v['id']:<24} {v['type']:<9} {v['releaseTime'][:10]}{mark}")
    print(f"\nпоследняя: {man['latest']['release']} (снапшот {man['latest']['snapshot']})")
    return 0


def _mc_options(args) -> mc_launch.Options:
    return mc_launch.Options(memory_mb=args.ram, resolution=args.resolution, java_path=args.java,
                             demo=args.demo, server=args.server, extra_jvm=args.jvm_arg or [])


def _prepare(layout: Layout, version: str, opts: mc_launch.Options) -> mc_launch.Prepared:
    bar = ProgressBar()
    try:
        return mc_launch.prepare(layout, version, opts, progress=bar.stage, log=info)
    finally:
        bar.finish()


def cmd_mc_install(args, layout: Layout) -> int:
    opts = mc_launch.Options(java_path=args.java)
    prep = _prepare(layout, args.version, opts)
    info(f"Minecraft {prep.version_id} готов. Запуск: techlauncher mc start {prep.version_id} -u НИК")
    return 0


def _pick_account(args, layout: Layout, settings: Settings) -> mc_auth.Account:
    if args.account:
        store = mc_auth.AccountStore(layout)
        acc = store.find(args.account)
        if acc is None:
            raise LauncherError(f"аккаунт '{args.account}' не найден (techlauncher mc auth list)")
        return mc_auth.ensure_fresh(store, acc, settings.get("msa_client_id"))
    name = args.username or settings.get("username")
    if not name:
        raise LauncherError("укажите ник: -u НИК (или аккаунт Microsoft: -a ИМЯ)")
    return mc_auth.offline(name)


def cmd_mc_start(args, layout: Layout) -> int:
    settings = Settings(layout)
    account = _pick_account(args, layout, settings)
    opts = _mc_options(args)
    version = args.version or settings.get("mc_version") or "release"
    prep = _prepare(layout, version, opts)

    if account.kind == "offline":
        settings.set("username", account.username)
    settings.set("mc_version", prep.version_id)
    settings.set("ram", opts.memory_mb)
    settings.save()

    cmd = mc_launch.command(layout, prep, account, opts)
    info(f"Запуск Minecraft {prep.version_id} как {account.label}")
    if args.verbose or args.dry:
        info("  " + " ".join(f'"{c}"' if " " in c else c for c in mc_launch.mask_command(cmd, account)))
    if args.dry:
        return 0
    if args.no_wait:
        mc_launch.start(layout, prep, account, opts)
        return 0
    import subprocess
    proc = mc_launch.start(layout, prep, account, opts, {
        "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
        "universal_newlines": True, "errors": "replace"})
    fmt = mc_launch.LogFormatter()
    try:
        for line in proc.stdout:
            for out in fmt.feed(line):
                print(out, flush=True)
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = proc.wait()
    info(f"Minecraft завершился с кодом {code}")
    return 0


def cmd_mc_auth(args, layout: Layout) -> int:
    store = mc_auth.AccountStore(layout)
    if args.auth_cmd == "login":
        settings = Settings(layout)
        cid = mc_auth.client_id_from(args.client_id, settings.get("msa_client_id"))
        if args.client_id:
            settings.set("msa_client_id", args.client_id)
            settings.save()

        def show(message: str, url: str, code: str) -> None:
            info(f"Откройте {url} и введите код: {code}")
            if not args.no_browser:
                import webbrowser
                webbrowser.open(url)

        acc = mc_auth.login(cid, show)
        store.put(acc)
        info(f"Вход выполнен: {acc.username}. Запуск: techlauncher mc start -a {acc.username}")
        warn("на общем компьютере после игры выполните: techlauncher mc auth logout " + acc.username)
    elif args.auth_cmd == "list":
        if not store.accounts:
            info("нет сохранённых аккаунтов Microsoft")
        for a in store.accounts:
            info(f"{a.username}  {a.uuid}")
    elif args.auth_cmd == "logout":
        acc = store.find(args.account)
        if acc is None:
            raise LauncherError(f"аккаунт '{args.account}' не найден")
        store.remove(acc)
        info(f"выход выполнен, токены {acc.username} удалены с этого компьютера")
    return 0


# ------------------------------------------------------------------ SCP:CB


def cmd_scp(args, layout: Layout) -> int:
    c = args.scp_cmd
    if c == "versions":
        vs, aliases = scpcb.versions(layout)
        installed = {i.version for i in scpcb.list_instances(layout)}
        for v in vs.values():
            mark = "  [установлена]" if v.id in installed else ""
            size = fmt_size(v.size) if v.size else "?"
            print(f"{v.id:<18} {v.kind:<8} {size:>9}  {v.name}{mark}")
        print("псевдонимы: " + ", ".join(f"{a} -> {t}" for a, t in aliases.items()))
    elif c == "install":
        bar = ProgressBar()
        inst = scpcb.install(layout, args.version, args.name, args.force, args.keep_archive,
                             args.exe, progress=lambda b, t, f, tf: bar(b, t, f, tf), log=info)
        bar.finish()
        info(f"Запуск: techlauncher scp start {inst.name}")
    elif c == "import":
        inst = scpcb.import_path(layout, Path(args.path).expanduser(), args.name, args.exe,
                                 args.label, args.force, log=info)
        info(f"Запуск: techlauncher scp start {inst.name}")
    elif c == "list":
        items = scpcb.list_instances(layout)
        if not items:
            info("SCP:CB не установлена (techlauncher scp install)")
        for i in items:
            print(f"{i.name}  {i.version}  {i.dir}")
    elif c == "start":
        inst = scpcb.get_instance(layout, args.instance)
        result = scpcb.start(layout, inst, resolution=args.resolution, fullscreen=args.fullscreen,
                             skip_launcher=args.skip_launcher, extra=args.set, wine_path=args.wine,
                             vram=args.vram, startup_video=args.startup_video, dry=args.dry, log=info)
        info(f"Запуск SCP:CB '{inst.name}'")
        if args.dry:
            info("  " + " ".join(f'"{c}"' if " " in c else c for c in result))
            return 0
        if not args.no_wait:
            info(f"Игра завершилась с кодом {result.wait()}")
    elif c == "config":
        inst = scpcb.get_instance(layout, args.instance)
        if args.pairs:
            scpcb.apply_settings(inst, args.pairs, warn=warn)
            info(f"сохранено: {inst.options_ini}")
        else:
            from .ini import IniFile
            current = None
            for section, key, value in IniFile(inst.options_ini).items():
                if section != current:
                    print(f"[{section}]")
                    current = section
                print(f"  {key} = {value}")
    elif c == "remove":
        inst = scpcb.get_instance(layout, args.instance)
        if not args.yes:
            if not sys.stdin.isatty():
                raise LauncherError("для удаления без вопроса используйте -y")
            if input(f"Удалить '{inst.name}' вместе с сохранениями? [y/N] ").strip().lower() not in ("y", "д", "да", "yes"):
                info("отменено")
                return 0
        scpcb.remove(inst)
        info(f"удалено: {inst.name}")
    return 0


# ------------------------------------------------------------------ парсер


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="techlauncher",
                                description="Лаунчер Minecraft и SCP: Containment Breach для учебных ПК. "
                                            "Без команды открывается окно.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--main-dir", metavar="PATH", default=str(default_main_dir()),
                   help="папка лаунчера (по умолчанию %(default)s; или TECHL_MAIN_DIR)")
    p.add_argument("-v", "--verbose", action="store_true", help="подробный вывод")
    sub = p.add_subparsers(dest="command", metavar="КОМАНДА")

    s = sub.add_parser("gui", help="открыть окно лаунчера")
    s.set_defaults(func=cmd_gui)
    s = sub.add_parser("doctor", help="проверить компьютер")
    s.set_defaults(func=cmd_doctor)

    # --- Minecraft
    mc = sub.add_parser("mc", help="Minecraft").add_subparsers(dest="mc_cmd", metavar="ДЕЙСТВИЕ")
    mc.required = True
    s = mc.add_parser("versions", help="список версий")
    s.add_argument("filter", nargs="*")
    s.add_argument("--snapshots", action="store_true", help="показать снапшоты")
    s.add_argument("--old", action="store_true", help="показать альфы и беты")
    s.add_argument("--installed", action="store_true", help="только установленные")
    s.add_argument("-n", "--limit", type=int, default=30, help="сколько показать (%(default)s)")
    s.set_defaults(func=cmd_mc_versions)

    s = mc.add_parser("install", help="скачать версию, не запуская")
    s.add_argument("version", nargs="?", default="release", help="версия, release или snapshot")
    s.add_argument("--java", metavar="PATH", help="своя Java вместо Java от Mojang")
    s.set_defaults(func=cmd_mc_install)

    s = mc.add_parser("start", help="запустить (недостающее докачается)")
    s.add_argument("version", nargs="?", help="версия (по умолчанию последняя запущенная или release)")
    s.add_argument("-u", "--username", help="офлайн-ник (запоминается)")
    s.add_argument("-a", "--account", help="аккаунт Microsoft (см. mc auth list)")
    s.add_argument("--ram", type=int, default=2048, metavar="МБ", help="память для игры (%(default)s)")
    s.add_argument("--resolution", type=parse_resolution, metavar="WxH", help="размер окна")
    s.add_argument("--server", metavar="HOST[:PORT]", help="сразу зайти на сервер")
    s.add_argument("--java", metavar="PATH", help="своя Java вместо Java от Mojang")
    s.add_argument("--jvm-arg", action="append", metavar="ARG", help="доп. аргумент JVM (--jvm-arg=-X...)")
    s.add_argument("--demo", action="store_true", help="демо-режим")
    s.add_argument("--dry", action="store_true", help="подготовить и показать команду, не запуская")
    s.add_argument("--no-wait", action="store_true", help="не ждать закрытия игры")
    s.set_defaults(func=cmd_mc_start)

    auth = mc.add_parser("auth", help="аккаунты Microsoft").add_subparsers(dest="auth_cmd", metavar="ДЕЙСТВИЕ")
    auth.required = True
    s = auth.add_parser("login", help="войти (код в браузере)")
    s.add_argument("--client-id", help="Azure Client ID приложения (запоминается)")
    s.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    s.set_defaults(func=cmd_mc_auth)
    s = auth.add_parser("list", help="сохранённые аккаунты")
    s.set_defaults(func=cmd_mc_auth)
    s = auth.add_parser("logout", help="выйти и удалить токены с этого ПК")
    s.add_argument("account")
    s.set_defaults(func=cmd_mc_auth)

    # --- SCP:CB
    scp = sub.add_parser("scp", help="SCP: Containment Breach").add_subparsers(dest="scp_cmd", metavar="ДЕЙСТВИЕ")
    scp.required = True
    s = scp.add_parser("versions", help="доступные версии")
    s.set_defaults(func=cmd_scp)
    s = scp.add_parser("install", help="скачать и установить")
    s.add_argument("version", nargs="?", default="latest")
    s.add_argument("--name", help="имя копии (по умолчанию = версия)")
    s.add_argument("--exe", help="путь к exe внутри архива")
    s.add_argument("--force", action="store_true", help="переустановить, сохранив Saves и options.ini")
    s.add_argument("--keep-archive", action="store_true", help="не удалять архив после установки")
    s.set_defaults(func=cmd_scp)
    s = scp.add_parser("import", help="добавить мод из архива или папки")
    s.add_argument("path")
    s.add_argument("--name", required=True)
    s.add_argument("--exe")
    s.add_argument("--label", help="подпись версии")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_scp)
    s = scp.add_parser("list", help="установленные копии")
    s.set_defaults(func=cmd_scp)
    s = scp.add_parser("start", help="запустить")
    s.add_argument("instance", nargs="?")
    s.add_argument("--resolution", type=parse_resolution, metavar="WxH")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--fullscreen", dest="fullscreen", action="store_true", default=None)
    g.add_argument("--windowed", dest="fullscreen", action="store_false")
    s.add_argument("--skip-launcher", action="store_true", help="без встроенного окна настроек игры")
    s.add_argument("--set", type=parse_kv, action="append", metavar="[СЕКЦИЯ/]КЛЮЧ=ЗНАЧЕНИЕ")
    s.add_argument("--wine", metavar="PATH", help="[Linux] свой wine")
    s.add_argument("--vram", type=int, default=512, metavar="МБ",
                   help="[Linux] видеопамять для игры в Wine (%(default)s; 0 — не трогать)")
    s.add_argument("--startup-video", action="store_true", default=None,
                   help="показывать вступительное видео (на Linux по умолчанию выключено: под Wine из-за него игра вылетает)")
    s.add_argument("--dry", action="store_true")
    s.add_argument("--no-wait", action="store_true")
    s.set_defaults(func=cmd_scp)
    s = scp.add_parser("config", help="показать/изменить options.ini")
    s.add_argument("instance", nargs="?")
    s.add_argument("pairs", nargs="*", type=parse_kv, metavar="[СЕКЦИЯ/]КЛЮЧ=ЗНАЧЕНИЕ")
    s.set_defaults(func=cmd_scp)
    s = scp.add_parser("remove", help="удалить копию")
    s.add_argument("instance")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_scp)

    # --- Wine
    w = sub.add_parser("wine", help="[Linux] переносной Wine без sudo").add_subparsers(dest="wine_cmd", metavar="ДЕЙСТВИЕ")
    w.required = True
    s = w.add_parser("install", help="скачать или обновить Wine (без sudo)")
    s.add_argument("--tag", help="версия сборки (по умолчанию последняя стабильная)")
    s.add_argument("--file", metavar="PATH", help="уже скачанный wine-*-amd64-wow64.tar.xz")
    s.add_argument("--force", action="store_true", help="переустановить, даже если версия та же")
    s.set_defaults(func=cmd_wine)
    s = w.add_parser("status", help="какой Wine используется, состояние папки настроек")
    s.set_defaults(func=cmd_wine)
    s = w.add_parser("kill", help="закрыть зависшую игру и все программы Wine")
    s.set_defaults(func=cmd_wine)
    s = w.add_parser("reset", help="сбросить папку настроек Wine (сохранения не трогает)")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(func=cmd_wine)
    s = w.add_parser("remove", help="удалить переносной Wine")
    s.set_defaults(func=cmd_wine)
    return p


def main(argv: List[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except AttributeError:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scp" and args.scp_cmd == "config" and args.instance and "=" in args.instance:
        args.pairs = [parse_kv(args.instance), *args.pairs]
        args.instance = None
    if args.command is None:  # без команды — окно, а если его нельзя открыть — справка
        args.func = cmd_gui
    layout = Layout(Path(args.main_dir).expanduser())
    try:
        return args.func(args, layout)
    except LauncherError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nпрервано", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        if args.verbose:
            raise
        print(f"непредвиденная ошибка: {e!r} (подробнее: -v)", file=sys.stderr)
        return 1
