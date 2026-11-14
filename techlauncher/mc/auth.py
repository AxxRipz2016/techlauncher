"""Аккаунты Minecraft: офлайн-ник и вход через Microsoft.

Вход через Microsoft (как в PortableMC, см. https://minecraft.wiki/w/Microsoft_authentication):
    код устройства (device code) -> токен Microsoft -> Xbox Live -> XSTS
    -> токен Minecraft -> профиль игрока.

Нужен Azure Client ID — идентификатор вашего приложения в Azure, которому Mojang
разрешила доступ к Minecraft API. Без одобрения Mojang последний шаг вернёт 403.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable, List, Optional

from .. import LauncherError, net
from ..paths import Layout, load_json, save_json

DEVICE_CODE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
XBL_URL = "https://user.auth.xboxlive.com/user/authenticate"
XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MC_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MC_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
SCOPE = "XboxLive.signin offline_access"

NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

XSTS_ERRORS = {
    2148916233: "у этого аккаунта Microsoft нет профиля Xbox — создайте его на xbox.com",
    2148916235: "Xbox Live недоступен в вашей стране",
    2148916236: "аккаунту нужна проверка возраста на xbox.com",
    2148916237: "аккаунту нужна проверка возраста на xbox.com",
    2148916238: "детский аккаунт: его нужно добавить в семейную группу Microsoft",
}


@dataclass
class Account:
    kind: str                      # "offline" или "msa"
    username: str
    uuid: str                      # без дефисов
    access_token: str = "0"
    refresh_token: Optional[str] = None
    expires_at: float = 0.0
    xuid: str = ""

    @property
    def user_type(self) -> str:
        return "msa" if self.kind == "msa" else "legacy"

    @property
    def label(self) -> str:
        return f"{self.username} (Microsoft)" if self.kind == "msa" else f"{self.username} (офлайн)"


# ------------------------------------------------------------------ офлайн


def offline_uuid(name: str) -> str:
    """UUID как у самой игры для офлайн-ника: UUID v3 от "OfflinePlayer:<ник>"."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return uuid.UUID(bytes=bytes(digest)).hex


def offline(name: str) -> Account:
    name = name.strip()
    if not name:
        raise LauncherError("введите ник")
    if not NAME_RE.match(name):
        raise LauncherError("ник: от 3 до 16 символов, латиница, цифры и '_'")
    return Account(kind="offline", username=name, uuid=offline_uuid(name))


# ------------------------------------------------------------------ хранилище


class AccountStore:
    """accounts.json в папке лаунчера (только для владельца: там токены)."""

    def __init__(self, layout: Layout):
        self.path = layout.accounts_file
        data = load_json(self.path, {}) or {}
        self.accounts: List[Account] = []
        for raw in data.get("accounts", []):
            try:
                self.accounts.append(Account(**raw))
            except TypeError:
                continue

    def save(self) -> None:
        save_json(self.path, {"accounts": [asdict(a) for a in self.accounts]}, private=True)

    def find(self, query: str) -> Optional[Account]:
        q = query.lower().replace("-", "")
        for a in self.accounts:
            if a.uuid == q or a.username.lower() == q:
                return a
        return None

    def put(self, account: Account) -> None:
        self.accounts = [a for a in self.accounts if a.uuid != account.uuid]
        self.accounts.append(account)
        self.save()

    def remove(self, account: Account) -> None:
        self.accounts = [a for a in self.accounts if a.uuid != account.uuid]
        self.save()


# ------------------------------------------------------------------ Microsoft


def client_id_from(explicit: str | None, settings_value: str | None) -> str:
    cid = explicit or os.environ.get("TECHL_MSA_CLIENT_ID") or settings_value
    if not cid:
        raise LauncherError(
            "для входа через Microsoft нужен Azure Client ID вашего приложения, одобренного "
            "Mojang. Как его получить — в README (раздел «Вход через Microsoft»). "
            "Затем: techlauncher mc auth login --client-id ВАШ_ID (ID запомнится). "
            "Без этого можно играть с офлайн-ником."
        )
    return cid


def _check(status: int, data: dict, what: str) -> dict:
    if status >= 400:
        detail = data.get("error_description") or data.get("errorMessage") or data.get("error") \
            or data.get("raw") or status
        raise LauncherError(f"{what}: {detail}")
    return data


def _minecraft_from_ms_token(ms_token: str) -> tuple:
    """Токен Microsoft -> (токен Minecraft, срок жизни в секундах)."""
    status, xbl = net.request_json(XBL_URL, json_body={
        "Properties": {"AuthMethod": "RPS", "SiteName": "user.auth.xboxlive.com",
                       "RpsTicket": f"d={ms_token}"},
        "RelyingParty": "http://auth.xboxlive.com", "TokenType": "JWT"})
    _check(status, xbl, "Xbox Live")
    uhs = xbl["DisplayClaims"]["xui"][0]["uhs"]

    status, xsts = net.request_json(XSTS_URL, json_body={
        "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl["Token"]]},
        "RelyingParty": "rp://api.minecraftservices.com/", "TokenType": "JWT"})
    if status == 401:
        raise LauncherError(XSTS_ERRORS.get(xsts.get("XErr"), f"Xbox: ошибка {xsts.get('XErr')}"))
    _check(status, xsts, "XSTS")

    status, mc = net.request_json(MC_LOGIN_URL, json_body={
        "identityToken": f"XBL3.0 x={uhs};{xsts['Token']}"})
    if status == 403:
        raise LauncherError("Mojang не разрешила вход этому приложению (Client ID). Нужно "
                            "одобрение: https://aka.ms/mce-reviewappid — см. README")
    _check(status, mc, "Minecraft")
    return mc["access_token"], int(mc.get("expires_in", 86400))


def _profile(mc_token: str) -> dict:
    status, profile = net.request_json(MC_PROFILE_URL,
                                       headers={"Authorization": f"Bearer {mc_token}"})
    if status == 404:
        raise LauncherError("на этом аккаунте не куплен Minecraft: Java Edition")
    return _check(status, profile, "профиль Minecraft")


def _finish(ms: dict) -> Account:
    mc_token, expires = _minecraft_from_ms_token(ms["access_token"])
    profile = _profile(mc_token)
    return Account(kind="msa", username=profile["name"], uuid=profile["id"],
                   access_token=mc_token, refresh_token=ms.get("refresh_token"),
                   expires_at=time.time() + expires)


def login(client_id: str, show_code: Callable[[str, str, str], None],
          cancelled: Callable[[], bool] = lambda: False) -> Account:
    """Вход по коду устройства. show_code(сообщение, адрес, код) показывает код пользователю."""
    status, dc = net.request_json(DEVICE_CODE_URL, form={"client_id": client_id, "scope": SCOPE})
    _check(status, dc, "Microsoft (код устройства)")
    show_code(dc.get("message", ""), dc["verification_uri"], dc["user_code"])

    interval = int(dc.get("interval", 5))
    deadline = time.time() + int(dc.get("expires_in", 900))
    while time.time() < deadline:
        if cancelled():
            raise LauncherError("вход отменён")
        time.sleep(interval)
        status, tok = net.request_json(TOKEN_URL, form={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id, "device_code": dc["device_code"]})
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err == "authorization_declined":
            raise LauncherError("вход отклонён в браузере")
        if err == "expired_token":
            break
        _check(status, tok, "Microsoft")
        return _finish(tok)
    raise LauncherError("время на ввод кода истекло, попробуйте ещё раз")


def refresh(client_id: str, account: Account) -> Account:
    if not account.refresh_token:
        raise LauncherError("сессия устарела, войдите заново: techlauncher mc auth login")
    status, tok = net.request_json(TOKEN_URL, form={
        "grant_type": "refresh_token", "client_id": client_id,
        "refresh_token": account.refresh_token, "scope": SCOPE})
    if status >= 400:
        raise LauncherError("сессия Microsoft устарела, войдите заново: techlauncher mc auth login")
    return _finish(tok)


def ensure_fresh(store: AccountStore, account: Account, client_id: str | None) -> Account:
    """Обновить токен Microsoft, если он скоро истечёт."""
    if account.kind != "msa" or account.expires_at > time.time() + 300:
        return account
    if not client_id:
        raise LauncherError("токен входа истёк, а Client ID не задан — войдите заново")
    fresh = refresh(client_id, account)
    store.put(fresh)
    return fresh
