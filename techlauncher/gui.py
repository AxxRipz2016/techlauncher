"""Окно лаунчера на tkinter (входит в Python, ставить ничего не нужно).

Долгие операции (загрузка, вход, запуск) выполняются в отдельном потоке, а
сообщения в окно передаются через очередь: tkinter можно трогать только из
главного потока.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Callable, List, Optional

from . import LauncherError, __version__, doctor, scpcb, wine
from .mc import auth as mc_auth
from .mc import launch as mc_launch
from .mc import versions as mc_versions
from .paths import Layout, Settings

OFFLINE = "Офлайн-ник"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def run(layout: Layout) -> int:
    try:
        import tkinter as tk
    except ImportError:
        print("Окно недоступно: в этом Python нет tkinter (пакет python3-tkinter).\n"
              "Запустите через start.py — он сам скачает переносной Python с окном.\n"
              "Или используйте команды: python3 -m techlauncher --help")
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"Не удалось открыть окно ({e}). Используйте команды: python3 -m techlauncher --help")
        return 1
    App(root, layout)
    root.mainloop()
    return 0


def _fmt(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class App:
    def __init__(self, root, layout: Layout):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk = tk, ttk
        self.root = root
        self.layout = layout
        self.settings = Settings(layout)
        self.queue: "queue.Queue" = queue.Queue()
        self.busy = False
        self.cancel_login = False
        self.manifest: Optional[dict] = None

        root.title(f"TechLauncher {__version__}")
        root.geometry("760x560")
        root.minsize(640, 480)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=False, padx=8, pady=(8, 4))
        self.mc_tab = ttk.Frame(nb, padding=10)
        self.scp_tab = ttk.Frame(nb, padding=10)
        self.doc_tab = ttk.Frame(nb, padding=10)
        nb.add(self.mc_tab, text="Minecraft")
        nb.add(self.scp_tab, text="SCP: Containment Breach")
        nb.add(self.doc_tab, text="Проверка ПК")
        self._build_mc()
        self._build_scp()
        self._build_doctor()

        bottom = ttk.Frame(root, padding=(8, 0, 8, 8))
        bottom.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Готово")
        ttk.Label(bottom, textvariable=self.status).pack(anchor="w")
        self.bar = ttk.Progressbar(bottom, mode="determinate", maximum=1000)
        self.bar.pack(fill="x", pady=(2, 4))
        frame = ttk.Frame(bottom)
        frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(frame, height=10, wrap="word", state="disabled")
        scroll = ttk.Scrollbar(frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        self.run_task(self._load_versions, "Загрузка списка версий...")

    # ------------------------------------------------------------ общее

    def log(self, msg: str) -> None:
        self.queue.put(("log", msg))

    def progress(self, stage: str, done: int, total: int, files: int, total_files: int) -> None:
        self.queue.put(("progress", stage, done, total, files, total_files))

    def call(self, fn: Callable, *args) -> None:
        """Выполнить fn в главном потоке (для обновления виджетов)."""
        self.queue.put(("call", fn, args))

    def run_task(self, fn: Callable, status: str) -> None:
        if self.busy:
            self.log("Подождите, предыдущее действие ещё выполняется")
            return
        self.busy = True
        self.status.set(status)

        def worker() -> None:
            try:
                fn()
                self.queue.put(("done", None))
            except LauncherError as e:
                self.queue.put(("done", str(e)))
            except Exception as e:  # noqa: BLE001
                self.log(traceback.format_exc())
                self.queue.put(("done", f"непредвиденная ошибка: {e!r}"))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append(item[1])
                elif kind == "progress":
                    _, stage, done, total, files, total_files = item
                    frac = done / total if total else files / max(total_files, 1)
                    self.bar["value"] = int(frac * 1000)
                    self.status.set(f"{stage}: {_fmt(done)} из {_fmt(total)}, файлов {files}/{total_files}")
                elif kind == "call":
                    item[1](*item[2])
                elif kind == "done":
                    self.busy = False
                    self.bar["value"] = 0
                    if item[1]:
                        self.status.set("Ошибка")
                        self._append(f"ОШИБКА: {item[1]}")
                        from tkinter import messagebox
                        messagebox.showerror("TechLauncher", item[1])
                    else:
                        self.status.set("Готово")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _append(self, msg: str) -> None:
        t = self.log_text
        t.configure(state="normal")
        t.insert("end", msg.rstrip() + "\n")
        lines = int(t.index("end-1c").split(".")[0])
        if lines > 3000:
            t.delete("1.0", f"{lines - 3000}.0")
        t.see("end")
        t.configure(state="disabled")

    def _watch(self, proc: subprocess.Popen, name: str, minecraft: bool = False) -> None:
        """Читать вывод игры в журнал, пока она работает (в отдельном потоке)."""
        def reader() -> None:
            fmt = mc_launch.LogFormatter() if minecraft else None
            if proc.stdout is not None:
                for line in proc.stdout:
                    for out in (fmt.feed(line) if fmt else [line]):
                        self.log(out)
            self.log(f"{name} завершилась с кодом {proc.wait()}")
        threading.Thread(target=reader, daemon=True).start()

    def _popen_kwargs(self) -> dict:
        return {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                "universal_newlines": True, "errors": "replace", "creationflags": NO_WINDOW}

    @staticmethod
    def _open_folder(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _on_close(self) -> None:
        self.cancel_login = True
        self._save_settings()
        self.root.destroy()

    def _save_settings(self) -> None:
        s = self.settings
        s.set("username", self.nick.get().strip())
        s.set("mc_version", self.version.get().strip())
        s.set("snapshots", bool(self.snapshots.get()))
        try:
            s.set("ram", int(self.ram.get()))
        except ValueError:
            pass
        s.set("account", self.account.get())
        s.set("scp_instance", self.scp_instance.get())
        s.set("scp_resolution", self.scp_res.get())
        s.set("scp_fullscreen", bool(self.scp_full.get()))
        s.set("scp_skip_launcher", bool(self.scp_skip.get()))
        s.save()

    # ------------------------------------------------------------ Minecraft

    def _build_mc(self) -> None:
        tk, ttk, f, s = self.tk, self.ttk, self.mc_tab, self.settings
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Версия:").grid(row=0, column=0, sticky="w", pady=3)
        self.version = tk.StringVar(value=s.get("mc_version", "release"))
        self.version_box = ttk.Combobox(f, textvariable=self.version)
        self.version_box.grid(row=0, column=1, sticky="ew", padx=6)
        self.snapshots = tk.BooleanVar(value=s.get("snapshots", False))
        ttk.Checkbutton(f, text="снапшоты", variable=self.snapshots,
                        command=self._fill_versions).grid(row=0, column=2, sticky="w")

        ttk.Label(f, text="Аккаунт:").grid(row=1, column=0, sticky="w", pady=3)
        self.account = tk.StringVar(value=s.get("account", OFFLINE))
        self.account_box = ttk.Combobox(f, textvariable=self.account, state="readonly")
        self.account_box.grid(row=1, column=1, sticky="ew", padx=6)
        self.account_box.bind("<<ComboboxSelected>>", lambda e: self._account_changed())
        ttk.Button(f, text="Войти через Microsoft", command=self._login).grid(row=1, column=2, sticky="ew")

        ttk.Label(f, text="Ник:").grid(row=2, column=0, sticky="w", pady=3)
        self.nick = tk.StringVar(value=s.get("username", ""))
        self.nick_entry = ttk.Entry(f, textvariable=self.nick)
        self.nick_entry.grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="Выйти из аккаунта", command=self._logout).grid(row=2, column=2, sticky="ew")

        ttk.Label(f, text="Память, МБ:").grid(row=3, column=0, sticky="w", pady=3)
        self.ram = tk.StringVar(value=str(s.get("ram", 2048)))
        ttk.Spinbox(f, from_=1024, to=16384, increment=512, textvariable=self.ram,
                    width=8).grid(row=3, column=1, sticky="w", padx=6)

        buttons = ttk.Frame(f)
        buttons.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="▶  Играть", command=self._mc_play).pack(side="left", ipadx=20, ipady=4)
        ttk.Button(buttons, text="Папка игры (моды, сохранения)",
                   command=lambda: self._open_folder(self.layout.mc_game)).pack(side="left", padx=8)
        self._fill_accounts()

    def _load_versions(self) -> None:
        self.manifest = mc_versions.manifest(self.layout)
        if self.manifest is None:
            self.log("Нет связи с Mojang: доступны только установленные версии")
        self.call(self._fill_versions)
        self.call(self._fill_scp)

    def _fill_versions(self) -> None:
        installed = mc_versions.installed(self.layout)
        values: List[str] = ["release"]
        if self.manifest:
            types = {"release", "snapshot"} if self.snapshots.get() else {"release"}
            values += [v["id"] for v in self.manifest["versions"] if v["type"] in types][:150]
        values += [v for v in installed if v not in values]
        self.version_box["values"] = values

    def _fill_accounts(self) -> None:
        store = mc_auth.AccountStore(self.layout)
        values = [OFFLINE] + [a.label for a in store.accounts]
        self.account_box["values"] = values
        if self.account.get() not in values:
            self.account.set(OFFLINE)
        self._account_changed()

    def _account_changed(self) -> None:
        self.nick_entry.configure(state="normal" if self.account.get() == OFFLINE else "disabled")

    def _selected_account(self) -> mc_auth.Account:
        if self.account.get() == OFFLINE:
            return mc_auth.offline(self.nick.get())
        store = mc_auth.AccountStore(self.layout)
        for a in store.accounts:
            if a.label == self.account.get():
                return mc_auth.ensure_fresh(store, a, self.settings.get("msa_client_id"))
        raise LauncherError("аккаунт не найден, войдите заново")

    def _mc_play(self) -> None:
        self._save_settings()
        try:
            ram = int(self.ram.get())
        except ValueError:
            ram = 2048
        version = self.version.get().strip() or "release"
        opts = mc_launch.Options(memory_mb=ram)

        def task() -> None:
            account = self._selected_account()
            prep = mc_launch.prepare(self.layout, version, opts, self.progress, self.log)
            self.log(f"Запуск Minecraft {prep.version_id} как {account.label}")
            proc = mc_launch.start(self.layout, prep, account, opts, self._popen_kwargs())
            self.call(self._fill_versions)
            self._watch(proc, "Minecraft", minecraft=True)

        self.run_task(task, f"Подготовка Minecraft {version}...")

    def _login(self) -> None:
        from tkinter import simpledialog
        cid = self.settings.get("msa_client_id")
        if not cid:
            cid = simpledialog.askstring(
                "Вход через Microsoft",
                "Нужен Azure Client ID приложения, одобренного Mojang\n"
                "(как получить — в README, раздел «Вход через Microsoft»).\n\nClient ID:",
                parent=self.root)
            if not cid:
                return
            self.settings.set("msa_client_id", cid.strip())
            self.settings.save()
        self.cancel_login = False

        def task() -> None:
            acc = mc_auth.login(self.settings.get("msa_client_id"),
                                lambda m, url, code: self.call(self._show_code, url, code),
                                cancelled=lambda: self.cancel_login)
            mc_auth.AccountStore(self.layout).put(acc)
            self.log(f"Вход выполнен: {acc.username}. На общем ПК не забудьте «Выйти из аккаунта».")
            self.call(self._after_login, acc.label)

        self.run_task(task, "Вход через Microsoft...")

    def _show_code(self, url: str, code: str) -> None:
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title("Вход через Microsoft")
        win.transient(self.root)
        ttk.Label(win, text=f"1. Откройте {url}\n2. Введите код:", padding=10).pack()
        entry = ttk.Entry(win, font=("TkFixedFont", 18), justify="center")
        entry.insert(0, code)
        entry.configure(state="readonly")
        entry.pack(padx=10, fill="x")
        self.root.clipboard_clear()
        self.root.clipboard_append(code)
        ttk.Label(win, text="(код уже скопирован)", padding=4).pack()
        row = ttk.Frame(win, padding=10)
        row.pack()

        def open_browser() -> None:
            import webbrowser
            webbrowser.open(url)

        def cancel() -> None:
            self.cancel_login = True
            win.destroy()

        ttk.Button(row, text="Открыть браузер", command=open_browser).pack(side="left", padx=4)
        ttk.Button(row, text="Отмена", command=cancel).pack(side="left", padx=4)
        self.code_window = win
        open_browser()

    def _after_login(self, label: str) -> None:
        win = getattr(self, "code_window", None)
        if win is not None and win.winfo_exists():
            win.destroy()
        self._fill_accounts()
        self.account.set(label)
        self._account_changed()

    def _logout(self) -> None:
        if self.account.get() == OFFLINE:
            return
        store = mc_auth.AccountStore(self.layout)
        for a in store.accounts:
            if a.label == self.account.get():
                store.remove(a)
                self.log(f"Выход выполнен: токены {a.username} удалены с этого компьютера")
        self.account.set(OFFLINE)
        self._fill_accounts()

    # ------------------------------------------------------------ SCP:CB

    def _build_scp(self) -> None:
        tk, ttk, f, s = self.tk, self.ttk, self.scp_tab, self.settings
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Копия игры:").grid(row=0, column=0, sticky="w", pady=3)
        self.scp_instance = tk.StringVar(value=s.get("scp_instance", ""))
        self.scp_box = ttk.Combobox(f, textvariable=self.scp_instance, state="readonly")
        self.scp_box.grid(row=0, column=1, sticky="ew", padx=6)

        ttk.Label(f, text="Разрешение:").grid(row=1, column=0, sticky="w", pady=3)
        self.scp_res = tk.StringVar(value=s.get("scp_resolution", "1280x720"))
        ttk.Combobox(f, textvariable=self.scp_res, values=scpcb.RESOLUTIONS).grid(
            row=1, column=1, sticky="w", padx=6)
        self.scp_full = tk.BooleanVar(value=s.get("scp_fullscreen", False))
        ttk.Checkbutton(f, text="Полный экран", variable=self.scp_full).grid(row=2, column=1, sticky="w", padx=6)
        self.scp_skip = tk.BooleanVar(value=s.get("scp_skip_launcher", True))
        ttk.Checkbutton(f, text="Без встроенного окна настроек игры",
                        variable=self.scp_skip).grid(row=3, column=1, sticky="w", padx=6)

        buttons = ttk.Frame(f)
        buttons.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="▶  Играть", command=self._scp_play).pack(side="left", ipadx=20, ipady=4)
        ttk.Button(buttons, text="Скачать игру (235 МБ)", command=self._scp_install).pack(side="left", padx=8)
        ttk.Button(buttons, text="Добавить мод (архив)...", command=self._scp_import).pack(side="left")
        if wine.needs_wine():
            ttk.Label(f, text="На Linux игра запускается через Wine; если его нет, лаунчер "
                              "предложит скачать (без прав администратора).",
                      foreground="gray").grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
            wb = ttk.Frame(f)
            wb.grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))
            ttk.Button(wb, text="Закрыть зависшую игру", command=self._wine_kill).pack(side="left")
            ttk.Button(wb, text="Обновить Wine", command=self._wine_update).pack(side="left", padx=8)
            ttk.Button(wb, text="Сбросить настройки Wine", command=self._wine_reset).pack(side="left")

    def _wine_kill(self) -> None:
        def task() -> None:
            done = wine.kill(wine.resolve(self.layout), self.layout.wine_prefix)
            self.log("Программы Wine закрыты" if done else "Запущенных программ Wine нет")
        self.run_task(task, "Закрываю программы Wine...")

    def _wine_update(self) -> None:
        def task() -> None:
            wine.install(self.layout, progress=lambda b, t, f, tf: self.progress("Wine", b, t, f, tf),
                         log=self.log)
        self.run_task(task, "Проверка обновлений Wine...")

    def _wine_reset(self) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno("Wine", "Сбросить папку настроек Wine? Это помогает, если игра "
                                           "перестала запускаться. Сохранения не пострадают."):
            return

        def task() -> None:
            self.log("Папка настроек Wine удалена, при запуске создастся заново"
                     if wine.reset_prefix(self.layout) else "Папки настроек Wine ещё нет")
        self.run_task(task, "Сброс настроек Wine...")

    def _fill_scp(self) -> None:
        names = [i.name for i in scpcb.list_instances(self.layout)]
        self.scp_box["values"] = names
        if self.scp_instance.get() not in names:
            self.scp_instance.set(names[0] if names else "")

    def _scp_install(self) -> None:
        def task() -> None:
            inst = scpcb.install(self.layout, "latest", force=False,
                                 progress=lambda b, t, f, tf: self.progress("SCP:CB", b, t, f, tf),
                                 log=self.log)
            self.call(self._fill_scp)
            self.call(self.scp_instance.set, inst.name)
        self.run_task(task, "Скачивание SCP:CB...")

    def _scp_import(self) -> None:
        from tkinter import filedialog, simpledialog
        path = filedialog.askopenfilename(
            title="Архив с модом SCP:CB",
            filetypes=[("Архивы", "*.zip *.7z *.rar *.tar.gz *.tar.xz"), ("Все файлы", "*")])
        if not path:
            return
        name = simpledialog.askstring("Имя копии", "Короткое имя (латиница, без пробелов):",
                                      parent=self.root)
        if not name:
            return

        def task() -> None:
            inst = scpcb.import_path(self.layout, Path(path), name.strip(), log=self.log)
            self.call(self._fill_scp)
            self.call(self.scp_instance.set, inst.name)
        self.run_task(task, "Импорт мода...")

    def _scp_play(self) -> None:
        self._save_settings()
        if not self.scp_instance.get():
            from tkinter import messagebox
            if messagebox.askyesno("SCP:CB", "Игра ещё не скачана. Скачать сейчас (235 МБ)?"):
                self._scp_install()
            return
        need_wine = False
        if wine.needs_wine():
            try:
                wine.find_wine(self.layout)
            except LauncherError:
                from tkinter import messagebox
                if not messagebox.askyesno("Wine", "Для SCP:CB на Linux нужен Wine. Скачать "
                                                   "переносной Wine (~95 МБ, без прав администратора)?"):
                    return
                need_wine = True
        try:
            w, h = (int(x) for x in self.scp_res.get().lower().split("x"))
            resolution = (w, h)
        except ValueError:
            resolution = None
        name = self.scp_instance.get()
        fullscreen, skip = bool(self.scp_full.get()), bool(self.scp_skip.get())

        def task() -> None:
            if need_wine:
                wine.install(self.layout, progress=lambda b, t, f, tf: self.progress("Wine", b, t, f, tf),
                             log=self.log)
            inst = scpcb.get_instance(self.layout, name)
            self.log(f"Запуск SCP:CB '{inst.name}'")
            proc = scpcb.start(self.layout, inst, resolution=resolution, fullscreen=fullscreen,
                               skip_launcher=skip, log=self.log, popen_kwargs=self._popen_kwargs())
            self._watch(proc, "SCP:CB")
        self.run_task(task, "Запуск SCP:CB...")

    # ------------------------------------------------------------ проверка ПК

    def _build_doctor(self) -> None:
        tk, ttk, f = self.tk, self.ttk, self.doc_tab
        ttk.Button(f, text="Проверить компьютер", command=self._doctor).pack(anchor="w")
        self.doc_text = tk.Text(f, height=12, wrap="word", state="disabled")
        self.doc_text.pack(fill="both", expand=True, pady=(6, 0))

    def _doctor(self) -> None:
        def task() -> None:
            checks = doctor.run(self.layout)
            lines = []
            for c in checks:
                lines.append(f"[{c.status}] {c.title}: {c.detail}")
                if c.advice:
                    lines.append(f"      -> {c.advice}")
            self.call(self._show_doctor, "\n".join(lines))
        self.run_task(task, "Проверка компьютера...")

    def _show_doctor(self, text: str) -> None:
        t = self.doc_text
        t.configure(state="normal")
        t.delete("1.0", "end")
        t.insert("end", text)
        t.configure(state="disabled")
