#!/usr/bin/env python3
"""初音的青葱 · 自动登录 / 每日访问 / 守护灵寻宝

  POST /sign     email + md5(password) -> token
  GET  /addJf    X-Auth-Token         -> 每日访问硬币
  GET  /hunt     X-Auth-Token         -> 守护灵寻宝
  GET  /getVip   X-Auth-Token         -> 账号信息

  python3 main.py
  python3 main.py --dry-run
  python3 main.py -c /path/config.yaml
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml

HUNT_STOP_CODES = {602, 604, 688}
CHECKIN_OK_CODES = {0, 10}
HUNT_OK_CODES = {0, 200}

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

MAX_RESPONSE_BYTES = 1_048_576
MAX_HUNT_ITEM_BYTES = 4096
MAX_HUNT_ITEMS_KEEP = 5
MAX_HUNT_MAX = 50
MAX_ACCOUNTS = 20
MAX_TIMEOUT = 60.0
MIN_TIMEOUT = 1.0
DEFAULT_JOB_MAX_SECONDS = 540
MAX_JOB_MAX_SECONDS = 540
MIN_JOB_MAX_SECONDS = 30
MAX_RETRIES_IDEMPOTENT = 3
RETRY_BACKOFF = 0.6
WARMUP_MAX_REDIRECTS = 3
ACCOUNT_GAP_SECONDS = 0.8
MIN_REQUEST_TIMEOUT = 0.5
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_RETENTION_DAYS = 14
LOG_PREVIEW_LEN = 160

LOG = logging.getLogger("chuyin")


class RequestError(RuntimeError):
    pass


class ConfigError(ValueError):
    pass


class JobTimeoutError(RuntimeError):
    pass


@dataclass
class AccountResult:
    email: str
    ok: bool = False
    nickname: str = ""
    lv: int | None = None
    jf: int | None = None
    jf2: int | None = None
    checkin: str = "skip"
    hunt_ok: int = 0
    hunt_items: list[Any] = field(default_factory=list)
    hunt_last: str = ""
    error: str = ""
    checkin_failed: bool = False
    hunt_failed: bool = False
    login_ok: bool = False


class Deadline:
    def __init__(self, max_seconds: float) -> None:
        self.max_seconds = float(max_seconds)
        self.start = time.monotonic()

    def remaining(self) -> float:
        return self.max_seconds - (time.monotonic() - self.start)

    def check(self, where: str = "") -> None:
        if self.remaining() <= 0:
            raise JobTimeoutError(
                f"超过 job_max_seconds={self.max_seconds:.0f}s"
                + (f" @ {where}" if where else "")
            )

    def sleep(self, seconds: float, where: str = "") -> None:
        if seconds <= 0:
            self.check(where)
            return
        self.check(where)
        rem = self.remaining()
        if seconds > rem:
            raise JobTimeoutError(
                f"休眠 {seconds}s 将超过剩余时限 {rem:.1f}s"
                + (f" @ {where}" if where else "")
            )
        time.sleep(seconds)


class ChuyinClient:
    def __init__(
        self,
        domain: str,
        timeout: float = 20,
        max_body: int = MAX_RESPONSE_BYTES,
        max_retries: int = MAX_RETRIES_IDEMPOTENT,
        session: requests.Session | None = None,
        deadline: Deadline | None = None,
    ) -> None:
        self.base = f"https://{domain.rstrip('/')}"
        self.base_host = urlparse(self.base).netloc.lower()
        self.timeout = timeout
        self.max_body = max_body
        self.max_retries = max_retries
        self.deadline = deadline
        self.token = ""
        self.s = session or requests.Session()
        self.s.headers.update(
            {
                "User-Agent": DEFAULT_UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Origin": self.base,
                "Referer": f"{self.base}/",
            }
        )

    def _auth_headers(self, auth: bool) -> dict[str, str]:
        if auth and self.token:
            return {"X-Auth-Token": self.token}
        return {}

    def _effective_timeout(self) -> float:
        if self.deadline is None:
            return self.timeout
        rem = self.deadline.remaining()
        if rem < MIN_REQUEST_TIMEOUT:
            raise JobTimeoutError("请求前剩余时限不足")
        return min(self.timeout, rem)

    def _read_capped_body(self, r: requests.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in r.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > self.max_body:
                    raise RequestError(
                        f"响应超过上限 {self.max_body} bytes "
                        f"path={getattr(r.request, 'path_url', '?')!r}"
                    )
                chunks.append(chunk)
        finally:
            r.close()
        return b"".join(chunks)

    def _raw_once(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: dict | None = None,
    ) -> tuple[requests.Response, bytes]:
        timeout = self._effective_timeout()
        kw: dict[str, Any] = {
            "headers": headers,
            "timeout": timeout,
            "stream": True,
            "allow_redirects": False,
        }
        if method.upper() == "POST":
            r = self.s.post(url, data=data or {}, **kw)
        else:
            r = self.s.get(url, **kw)
        return r, self._read_capped_body(r)

    def _retry_sleep(self, attempt: int) -> None:
        delay = RETRY_BACKOFF * attempt
        if self.deadline is not None:
            self.deadline.sleep(delay, "retry")
        else:
            time.sleep(delay)

    def _request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = False,
        data: dict | None = None,
        allow_retry: bool = True,
    ) -> Any:
        url = f"{self.base}{path}"
        headers = self._auth_headers(auth)
        if method.upper() == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        attempts = self.max_retries if allow_retry else 1
        last_err: Exception | None = None

        for attempt in range(1, attempts + 1):
            if self.deadline is not None:
                self.deadline.check(path)
            try:
                r, body = self._raw_once(method, url, headers=headers, data=data)
                status = r.status_code

                if 300 <= status < 400:
                    loc = r.headers.get("Location", "")
                    raise RequestError(
                        f"{path} 拒绝重定向 HTTP {status} Location={loc!r}"
                    )
                if not (200 <= status < 300):
                    raise RequestError(f"{path} HTTP {status} body={body[:200]!r}")

                ctype = (r.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype:
                    raise RequestError(
                        f"{path} 返回 HTML（未登录/域名错误/路由变更） status={status}"
                    )

                text = body.decode("utf-8", errors="replace")
                try:
                    return json.loads(text) if text else {}
                except json.JSONDecodeError as e:
                    raise RequestError(
                        f"{path} 非 JSON: status={status} body={body[:200]!r}"
                    ) from e

            except JobTimeoutError:
                raise
            except (requests.RequestException, RequestError) as e:
                last_err = e
                if attempt >= attempts:
                    break
                msg = str(e)
                if any(
                    x in msg
                    for x in (
                        "响应超过上限",
                        "拒绝重定向",
                        "HTTP 4",
                        "非 JSON",
                        "返回 HTML",
                    )
                ):
                    break
                retryable = isinstance(e, requests.RequestException) or "HTTP 5" in msg
                if not retryable:
                    break
                self._retry_sleep(attempt)

        assert last_err is not None
        suffix = f"(重试{attempts}次)" if attempts > 1 else ""
        raise RequestError(f"{path} 请求失败{suffix}: {last_err}") from last_err

    def warmup(self) -> None:
        url = f"{self.base}/"
        for hop in range(WARMUP_MAX_REDIRECTS + 1):
            if self.deadline is not None:
                self.deadline.check("warmup")
            r, body = self._raw_once("GET", url, headers={})
            status = r.status_code
            if 200 <= status < 300:
                return
            if not (300 <= status < 400):
                raise RequestError(f"warmup HTTP {status} body={body[:200]!r}")
            loc = r.headers.get("Location") or ""
            if not loc:
                raise RequestError(f"warmup 重定向无 Location status={status}")
            next_url = urljoin(url, loc)
            parsed = urlparse(next_url)
            if parsed.scheme not in ("http", "https"):
                raise RequestError(f"warmup 非法跳转 scheme: {next_url!r}")
            if parsed.netloc.lower() != self.base_host:
                raise RequestError(
                    f"warmup 拒绝跨 host 跳转: {self.base_host} -> {parsed.netloc}"
                )
            if hop >= WARMUP_MAX_REDIRECTS:
                raise RequestError(
                    f"warmup 超过最大跳转 {WARMUP_MAX_REDIRECTS}: {next_url}"
                )
            url = next_url
        raise RequestError("warmup 失败")

    def login(self, email: str, password: str) -> dict:
        pwd_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
        data = self._request(
            "POST",
            "/sign",
            data={"email": email, "password": pwd_md5},
            allow_retry=True,
        )
        if not isinstance(data, dict):
            raise RequestError(f"登录响应非 object: {type(data)}")
        token = _extract_token(data)
        if not token:
            raise RequestError(
                f"登录失败 code={data.get('code')!r} msg={data.get('msg')!r}"
            )
        self.token = token
        return data

    def add_jf(self) -> dict:
        data = self._request("GET", "/addJf", auth=True, allow_retry=False)
        if not isinstance(data, dict):
            raise RequestError("addJf 响应非 object")
        return data

    def hunt(self) -> dict:
        data = self._request("GET", "/hunt", auth=True, allow_retry=False)
        if not isinstance(data, dict):
            raise RequestError("hunt 响应非 object")
        return data

    def get_vip(self) -> dict:
        data = self._request("GET", "/getVip", auth=True, allow_retry=True)
        if not isinstance(data, dict):
            raise RequestError("getVip 响应非 object")
        return data


def _extract_token(data: dict) -> str | None:
    def from_mapping(m: Any) -> str | None:
        if not isinstance(m, dict):
            return None
        t = m.get("token")
        return t.strip() if isinstance(t, str) and t.strip() else None

    for key in (None, "data", "obj", "user"):
        node: Any = data if key is None else data.get(key)
        if key is not None and isinstance(node, str):
            try:
                node = json.loads(node)
            except (TypeError, ValueError, json.JSONDecodeError):
                node = None
        t = from_mapping(node)
        if t:
            return t
    return None


def _code_of(resp: dict) -> int | None:
    c = resp.get("code")
    if c is None or isinstance(c, bool):
        return None
    if isinstance(c, int):
        return c
    if isinstance(c, float):
        if not math.isfinite(c) or abs(c - round(c)) > 1e-9:
            return None
        return int(round(c))
    if isinstance(c, str):
        s = c.strip()
        if s.startswith("-"):
            body = s[1:]
            if not body.isdigit():
                return None
        elif not s.isdigit():
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _extract_vip(resp: dict) -> dict[str, Any]:
    obj = resp.get("obj") if isinstance(resp.get("obj"), dict) else {}
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    src = {**resp, **data, **obj}
    return {
        "nickname": src.get("nickname") or src.get("name") or "",
        "lv": src.get("lv"),
        "jf": src.get("jf"),
        "jf2": src.get("jf2"),
    }


def _extract_hunt_items(resp: dict) -> list[Any]:
    for key in ("obj", "data"):
        v = resp.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("obj"), list):
            return v["obj"]
    return []


def _item_size(it: Any) -> tuple[Any, int]:
    try:
        raw = json.dumps(it, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        raw = str(it)
    return raw, len(raw.encode("utf-8"))


def _clip_hunt_items(existing: list[Any], new_items: list[Any]) -> list[Any]:
    clipped: list[Any] = []
    for it in new_items:
        raw, nbytes = _item_size(it)
        if nbytes > MAX_HUNT_ITEM_BYTES:
            clipped.append({"_truncated": True, "preview": raw[:200]})
        else:
            clipped.append(it)
    return (existing + clipped)[-MAX_HUNT_ITEMS_KEEP:]


def _preview(obj: Any, limit: int = LOG_PREVIEW_LEN) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _safe_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v) or abs(v - round(v)) > 1e-9:
            return None
        return int(round(v))
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("-"):
            if not s[1:].isdigit():
                return None
        elif not s.isdigit():
            return None
        try:
            return int(s)
        except ValueError:
            return None
    return None


def _as_bool(name: str, v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    raise ConfigError(f"{name} 必须是 YAML 布尔值 true/false，收到 {v!r}")


def _finite_number(
    name: str, v: Any, *, lo: float, hi: float, default: float
) -> float:
    if v is None:
        return default
    if isinstance(v, bool):
        raise ConfigError(f"{name} 必须是数字，不能是布尔值")
    try:
        n = float(v)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{name} 必须是数字，收到 {v!r}") from e
    if not math.isfinite(n):
        raise ConfigError(f"{name} 必须是有限数字，收到 {v!r}")
    if n < lo or n > hi:
        raise ConfigError(f"{name} 范围 [{lo}, {hi}]，收到 {n}")
    return n


def _finite_int(name: str, v: Any, *, lo: int, hi: int, default: int) -> int:
    if v is None:
        return default
    if isinstance(v, bool):
        raise ConfigError(f"{name} 必须是整数，不能是布尔值")
    n = _finite_number(name, v, lo=lo, hi=hi, default=float(default))
    if abs(n - round(n)) > 1e-9:
        raise ConfigError(f"{name} 必须是整数，收到 {v!r}")
    return int(round(n))


def _apply_vip_info(res: AccountResult, vip: dict) -> None:
    info = _extract_vip(vip)
    res.nickname = str(info.get("nickname") or "")
    lv = info.get("lv")
    res.lv = lv if isinstance(lv, int) and not isinstance(lv, bool) else _safe_int(lv)
    res.jf = _safe_int(info.get("jf"))
    res.jf2 = _safe_int(info.get("jf2"))


def try_get_vip(client: ChuyinClient, res: AccountResult, label: str) -> None:
    try:
        vip = client.get_vip()
        _apply_vip_info(res, vip)
        LOG.info(
            "[%s] %s nickname=%s lv=%s 硬币=%s 积分=%s",
            res.email,
            label,
            res.nickname,
            res.lv,
            res.jf,
            res.jf2,
        )
    except Exception as e:
        LOG.warning("[%s] %s 失败(不阻断核心任务): %s", res.email, label, e)


def _run_checkin(client: ChuyinClient, res: AccountResult) -> None:
    try:
        r = client.add_jf()
        code = _code_of(r)
        if code in CHECKIN_OK_CODES:
            res.checkin = (
                f"ok +coin msg={r.get('msg', '')}" if code == 0 else "already"
            )
            LOG.info("[%s] 每日访问 %s", res.email, res.checkin)
            return
        res.checkin_failed = True
        res.checkin = f"fail code={code} body={_preview(r, 120)}"
        LOG.error("[%s] 每日访问失败 %s", res.email, res.checkin)
    except RequestError as e:
        res.checkin_failed = True
        res.checkin = f"error: {e}"
        LOG.error("[%s] 每日访问异常: %s", res.email, e)


def _run_hunt(
    client: ChuyinClient,
    res: AccountResult,
    *,
    hunt_max: int,
    hunt_interval: float,
    deadline: Deadline | None,
) -> None:
    stopped_ok = False
    try:
        for i in range(1, hunt_max + 1):
            if deadline is not None:
                deadline.check(f"hunt#{i}")
            r = client.hunt()
            code = _code_of(r)
            items = _extract_hunt_items(r)

            if code in HUNT_STOP_CODES:
                res.hunt_last = f"stop code={code} msg={r.get('msg', '')}"
                LOG.info("[%s] 寻宝停止 #%s %s", res.email, i, res.hunt_last)
                stopped_ok = True
                break

            if code in HUNT_OK_CODES:
                res.hunt_ok += 1
                if items:
                    res.hunt_items = _clip_hunt_items(res.hunt_items, items)
                preview = _preview(items[:3] if items else r.get("msg"))
                res.hunt_last = f"ok code={code} items={preview}"
                LOG.info("[%s] 寻宝成功 #%s %s", res.email, i, res.hunt_last)
            else:
                res.hunt_failed = True
                res.hunt_last = f"unknown code={code} body={_preview(r)}"
                LOG.error("[%s] 寻宝未知响应 #%s %s", res.email, i, res.hunt_last)
                break

            if i < hunt_max and hunt_interval > 0:
                if deadline is not None:
                    deadline.sleep(hunt_interval, f"hunt-interval#{i}")
                else:
                    time.sleep(hunt_interval)
        else:
            res.hunt_last = f"hit hunt_max={hunt_max}"
            if res.hunt_ok == 0:
                res.hunt_failed = True

        if not stopped_ok and res.hunt_ok == 0 and not res.hunt_failed:
            res.hunt_failed = True
            res.hunt_last = res.hunt_last or "no hunt attempts"
    except RequestError as e:
        res.hunt_failed = True
        res.hunt_last = f"error: {e}"
        LOG.error("[%s] 寻宝异常: %s", res.email, e)


def run_account(
    client: ChuyinClient,
    email: str,
    password: str,
    *,
    do_checkin: bool,
    do_hunt: bool,
    hunt_max: int,
    hunt_interval: float,
    dry_run: bool,
    deadline: Deadline | None = None,
) -> AccountResult:
    res = AccountResult(email=email)
    try:
        if deadline is not None:
            deadline.check("account-start")
        client.warmup()
        login_data = client.login(email, password)
        res.login_ok = True
        LOG.info("[%s] 登录成功 code=%s", email, _code_of(login_data))

        if dry_run or do_checkin or do_hunt:
            try_get_vip(client, res, "信息")

        if dry_run:
            res.checkin = "dry-run"
            res.hunt_last = "dry-run"
            res.ok = res.login_ok
            return res

        if do_checkin:
            _run_checkin(client, res)
        if do_hunt:
            _run_hunt(
                client,
                res,
                hunt_max=hunt_max,
                hunt_interval=hunt_interval,
                deadline=deadline,
            )

        if do_checkin or do_hunt:
            try_get_vip(client, res, "结束后信息")

        res.ok = res.login_ok and not res.checkin_failed and not res.hunt_failed
        if not res.ok and not res.error:
            parts = []
            if res.checkin_failed:
                parts.append(f"checkin={res.checkin}")
            if res.hunt_failed:
                parts.append(f"hunt={res.hunt_last}")
            res.error = "; ".join(parts) or "核心任务失败"
    except JobTimeoutError as e:
        res.error = str(e)
        res.ok = False
        LOG.error("[%s] 任务超时: %s", email, e)
    except RequestError as e:
        res.error = str(e)
        res.ok = False
        LOG.error("[%s] 失败: %s", email, e)
    except Exception as e:
        res.error = str(e)
        res.ok = False
        LOG.exception("[%s] 未预期异常: %s", email, e)
    return res


def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}\n请先: cp config.example.yaml config.yaml 并填写账号"
        )
    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise ConfigError("config 必须是 YAML mapping")
    return cfg


def parse_config(cfg: dict) -> dict[str, Any]:
    domain = cfg.get("domain", "www.yngal.com")
    if domain is None or not isinstance(domain, str) or not domain.strip():
        raise ConfigError("domain 必须是非空字符串")
    domain = domain.strip().removeprefix("https://").removeprefix("http://").strip("/")
    if not domain or "/" in domain or " " in domain or domain in (".", ".."):
        raise ConfigError(f"domain 非法: {domain!r}")

    timeout = _finite_number(
        "timeout", cfg.get("timeout"), lo=MIN_TIMEOUT, hi=MAX_TIMEOUT, default=20.0
    )
    hunt_max = _finite_int(
        "hunt_max", cfg.get("hunt_max"), lo=1, hi=MAX_HUNT_MAX, default=20
    )
    hunt_interval = _finite_number(
        "hunt_interval", cfg.get("hunt_interval"), lo=0.0, hi=30.0, default=1.5
    )
    job_max_seconds = _finite_number(
        "job_max_seconds",
        cfg.get("job_max_seconds"),
        lo=MIN_JOB_MAX_SECONDS,
        hi=MAX_JOB_MAX_SECONDS,
        default=float(DEFAULT_JOB_MAX_SECONDS),
    )
    global_checkin = _as_bool("do_checkin", cfg.get("do_checkin"), True)
    global_hunt = _as_bool("do_hunt", cfg.get("do_hunt"), True)

    accounts_raw = cfg.get("accounts")
    if accounts_raw is None:
        raise ConfigError("config.accounts 缺失")
    if not isinstance(accounts_raw, list):
        raise ConfigError(
            f"config.accounts 必须是列表，收到 {type(accounts_raw).__name__}"
        )
    if not accounts_raw:
        raise ConfigError("config.accounts 为空")
    if len(accounts_raw) > MAX_ACCOUNTS:
        raise ConfigError(f"accounts 最多 {MAX_ACCOUNTS} 个，收到 {len(accounts_raw)}")

    accounts: list[dict[str, Any]] = []
    for i, acc in enumerate(accounts_raw):
        if not isinstance(acc, dict):
            raise ConfigError(
                f"accounts[{i}] 必须是 mapping，收到 {type(acc).__name__}"
            )
        enabled = _as_bool(f"accounts[{i}].enabled", acc.get("enabled"), True)
        if not enabled:
            email = acc.get("email")
            email_s = (
                email.strip()
                if isinstance(email, str) and email.strip()
                else f"#{i}"
            )
            accounts.append(
                {
                    "email": email_s,
                    "password": "",
                    "enabled": False,
                    "do_checkin": global_checkin,
                    "do_hunt": global_hunt,
                }
            )
            continue
        email = acc.get("email")
        password = acc.get("password")
        if not isinstance(email, str) or not email.strip():
            raise ConfigError(f"accounts[{i}] 缺少有效 email")
        if not isinstance(password, str) or password == "":
            raise ConfigError(f"accounts[{i}] ({email.strip()}) 缺少 password")
        accounts.append(
            {
                "email": email.strip(),
                "password": password,
                "enabled": True,
                "do_checkin": _as_bool(
                    f"accounts[{i}].do_checkin",
                    acc.get("do_checkin"),
                    global_checkin,
                ),
                "do_hunt": _as_bool(
                    f"accounts[{i}].do_hunt", acc.get("do_hunt"), global_hunt
                ),
            }
        )

    enabled = [a for a in accounts if a.get("enabled", True)]
    if not enabled:
        raise ConfigError("没有启用的账号")

    n_en = len(enabled)
    n_hunt = sum(1 for a in enabled if a.get("do_hunt"))
    worst_sleep = n_hunt * max(0, hunt_max - 1) * hunt_interval
    if n_en > 1:
        worst_sleep += (n_en - 1) * ACCOUNT_GAP_SECONDS
    reqs_per = 4 + (hunt_max if n_hunt else 0)
    worst_req = n_en * reqs_per * timeout
    if worst_sleep + worst_req > job_max_seconds:
        raise ConfigError(
            f"配置超出 job 时限: 估算 worst≈{worst_sleep + worst_req:.0f}s "
            f"> job_max_seconds={job_max_seconds:.0f}s "
            f"(accounts={n_en}, hunt_max={hunt_max}, hunt_interval={hunt_interval}, "
            f"timeout={timeout}). 请降低 hunt_max/interval/账号数，"
            f"或提高 job_max_seconds（上限 {MAX_JOB_MAX_SECONDS}，需同步改 systemd RuntimeMaxSec）"
        )

    return {
        "domain": domain,
        "timeout": timeout,
        "hunt_max": hunt_max,
        "hunt_interval": hunt_interval,
        "job_max_seconds": job_max_seconds,
        "accounts": accounts,
    }


def setup_logging(log_dir: Path, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_dir, 0o700)
    except OSError:
        pass

    _prune_old_logs(log_dir, LOG_RETENTION_DAYS)

    day = datetime.now().strftime("%Y-%m-%d")
    logfile = log_dir / f"run-{day}.log"
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    fh = RotatingFileHandler(
        logfile,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    try:
        os.chmod(logfile, 0o600)
    except OSError:
        pass


def _prune_old_logs(log_dir: Path, keep_days: int) -> None:
    cutoff = datetime.now() - timedelta(days=keep_days)
    for p in log_dir.glob("run-*.log*"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                p.unlink(missing_ok=True)
        except OSError:
            continue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="初音的青葱 自动登录/签到/寻宝")
    parser.add_argument(
        "-c",
        "--config",
        default=os.environ.get("CHUYIN_CONFIG", "config.yaml"),
        help="配置文件路径 (默认 config.yaml，环境变量 CHUYIN_CONFIG)",
    )
    parser.add_argument("--dry-run", action="store_true", help="只登录查信息")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    base_dir = Path(__file__).resolve().parent
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = base_dir / cfg_path

    setup_logging(base_dir / "logs", args.verbose)

    try:
        cfg = parse_config(load_config(cfg_path))
    except (FileNotFoundError, ConfigError, yaml.YAMLError) as e:
        LOG.error("配置错误: %s", e)
        return 2

    domain = cfg["domain"]
    timeout = cfg["timeout"]
    hunt_max = cfg["hunt_max"]
    hunt_interval = cfg["hunt_interval"]
    job_max_seconds = cfg["job_max_seconds"]
    accounts = cfg["accounts"]
    enabled_accounts = [a for a in accounts if a.get("enabled", True)]
    deadline = Deadline(job_max_seconds)

    LOG.info(
        "==== 开始 domain=%s accounts=%d dry_run=%s job_max=%ss ====",
        domain,
        len(enabled_accounts),
        args.dry_run,
        int(job_max_seconds),
    )

    results: list[AccountResult] = []
    remaining_enabled = len(enabled_accounts)

    try:
        for acc in accounts:
            if not acc.get("enabled", True):
                LOG.info("跳过禁用账号 %s", acc.get("email"))
                continue

            remaining_enabled -= 1
            deadline.check(f"before {acc['email']}")
            client = ChuyinClient(domain=domain, timeout=timeout, deadline=deadline)
            results.append(
                run_account(
                    client,
                    acc["email"],
                    acc["password"],
                    do_checkin=acc["do_checkin"],
                    do_hunt=acc["do_hunt"],
                    hunt_max=hunt_max,
                    hunt_interval=hunt_interval,
                    dry_run=args.dry_run,
                    deadline=deadline,
                )
            )
            if remaining_enabled > 0:
                deadline.sleep(ACCOUNT_GAP_SECONDS, "account-gap")
    except JobTimeoutError as e:
        LOG.error("整次任务超时: %s", e)
        if not results:
            return 1

    if not results:
        LOG.error("没有执行任何账号")
        return 2

    LOG.info("==== 摘要 ====")
    fail = 0
    for r in results:
        if r.ok:
            LOG.info(
                "OK %s | %s lv=%s 硬币=%s 积分=%s | checkin=%s | hunt_ok=%s last=%s items=%s",
                r.email,
                r.nickname,
                r.lv,
                r.jf,
                r.jf2,
                r.checkin,
                r.hunt_ok,
                r.hunt_last,
                _preview(r.hunt_items),
            )
        else:
            fail += 1
            LOG.error("FAIL %s | %s", r.email, r.error or "unknown")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
