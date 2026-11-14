"""Своя реализация установки и запуска Minecraft (по образцу PortableMC).

Порядок работы (см. launch.prepare):
1. versions — манифест Mojang и JSON версии (с наследованием inheritsFrom);
2. install  — список файлов: библиотеки, нативные файлы, ассеты, client.jar, конфиг логов;
3. java     — подходящая Java от Mojang;
4. launch   — подстановка ${...} в аргументы и запуск java.
"""
