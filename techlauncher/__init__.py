"""TechLauncher — лаунчер для учебных ПК: Minecraft и SCP: Containment Breach.

Работает на Windows и Linux (в том числе без прав администратора).
"""

__version__ = "0.1.0"


class LauncherError(Exception):
    """Ожидаемая ошибка, которую интерфейс показывает пользователю без трейсбека."""
