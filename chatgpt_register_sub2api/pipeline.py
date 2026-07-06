"""Pipeline orchestrator — wires register → join → refresh/check → export.

The complete flow for one account:
  [1] Register account → get personal-scope tokens
  [2] Join parent K12 workspace → auto-accepted
  [3] Refresh/check account info, or explicit Team re-login when enabled
  [4] Export usable tokens as sub2api JSON

Each account proceeds independently through all 4 stages.
Results are written to registered_accounts.json after each success.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatgpt_register_sub2api.register.registrar import register_worker
from chatgpt_register_sub2api.workspace.joiner import join_workspaces
from chatgpt_register_sub2api.login.login_flow import re_login_for_team_token
from chatgpt_register_sub2api.export.sub2api import export_sub2api_json
from chatgpt_register_sub2api.utils.proxy import normalize_proxy_url

logger = logging.getLogger(__name__)

WORKSPACE_PLAN_TYPES = {
    "business",
    "education",
    "edu",
    "edu_plus",
    "edu_pro",
    "enterprise",
    "free_workspace",
    "k12",
    "quorum",
    "sci",
    "self_serve_business_usage_based",
    "team",
}
DEFAULT_WORKSPACE_EXPORT_PLAN = "k12"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def create_run_output_dir(config: dict[str, Any], count: int | None = None) -> Path:
    """Create a timestamped output folder for a full pipeline run."""
    config_dir = Path(config.get("_config_dir", "."))
    output_cfg = config.get("output", {})
    if not isinstance(output_cfg, dict):
        output_cfg = {}
    runs_dir_value = str(output_cfg.get("runs_dir") or "runs").strip() or "runs"
    runs_dir = Path(runs_dir_value)
    if not runs_dir.is_absolute():
        runs_dir = config_dir / runs_dir

    planned_count = _positive_int(
        count if count is not None else config.get("registration", {}).get("total"),
        1,
    )
    stem = f"{_timestamp()}_{planned_count}_accounts"
    run_dir = runs_dir / stem
    suffix = 2
    while run_dir.exists():
        run_dir = runs_dir / f"{stem}-{suffix}"
        suffix += 1
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _mail_config_with_proxy(config: dict[str, Any]) -> dict[str, Any]:
    mail_cfg = dict(config.get("mail", {}))
    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    if proxy and not mail_cfg.get("proxy"):
        mail_cfg["proxy"] = proxy
    return mail_cfg


def _resolve_export_output_path(
    config: dict[str, Any],
    output_file: str | Path | None = None,
) -> Path:
    if output_file:
        path = Path(output_file)
    else:
        sub2api_cfg = config.get("sub2api", {})
        configured = str(sub2api_cfg.get("output_file") or "").strip()
        path = Path(configured) if configured else Path(f"sub2api-{_timestamp()}.json")

    if not path.is_absolute():
        path = Path(config.get("_config_dir", ".")) / path
    return path


def _positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _parallel_threads(config: dict[str, Any], key: str, default: int = 1) -> int:
    parallel_cfg = config.get("parallel", {})
    if not isinstance(parallel_cfg, dict):
        parallel_cfg = {}
    return _positive_int(parallel_cfg.get(key, default), default)


def _bounded_workers(requested: int, item_count: int) -> int:
    return max(1, min(_positive_int(requested, 1), max(1, item_count)))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, tuple):
        raw_items = list(value)
    elif value is None:
        raw_items = []
    else:
        raw_items = [value]

    result: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def load_accounts(path: Path) -> list[dict[str, Any]]:
    """Load registered accounts from JSON file."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_accounts(path: Path, accounts: list[dict[str, Any]]) -> None:
    """Save accounts to JSON file (atomic write)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(accounts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _create_http_session(proxy: str = ""):
    from curl_cffi import requests

    kwargs = {"impersonate": "chrome", "verify": True}
    proxy_url = normalize_proxy_url(proxy)
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return requests.Session(**kwargs)


def _is_workspace_plan(plan: Any) -> bool:
    return str(plan or "").strip().lower() in WORKSPACE_PLAN_TYPES


def _fetch_account_context(
    session,
    access_token: str,
    workspace_id: str = "",
) -> dict[str, str]:
    resp = session.get(
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"check API failed: HTTP {resp.status_code}")

    data = resp.json() if resp.text else {}
    candidates = _extract_account_contexts(data)
    target_workspace = str(workspace_id or "").strip()
    selected = None
    if target_workspace:
        selected = next(
            (
                item
                for item in candidates
                if target_workspace in item.get("_ids", set())
            ),
            None,
        )
    if selected is None:
        selected = next((item for item in candidates if item.get("_default")), None)
    if selected is None and candidates:
        selected = candidates[0]
    if selected is None:
        return {
            "plan_type": "",
            "chatgpt_account_id": "",
            "account_user_role": "",
        }

    return {
        "plan_type": str(selected.get("plan_type") or "").strip(),
        "chatgpt_account_id": str(selected.get("chatgpt_account_id") or "").strip(),
        "account_user_role": str(selected.get("account_user_role") or "").strip(),
    }


def _extract_account_contexts(data: Any) -> list[dict[str, Any]]:
    """Extract account contexts from /accounts/check across known shapes."""
    contexts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(raw: Any, is_default: bool = False) -> None:
        if not isinstance(raw, dict):
            return
        wrapper = raw
        account = raw.get("account") if isinstance(raw.get("account"), dict) else raw
        ids = {
            str(value).strip()
            for source in (wrapper, account)
            for key in ("id", "account_id", "workspace_id")
            for value in [source.get(key)]
            if value
        }
        plan = str(account.get("plan_type") or wrapper.get("plan_type") or "").strip()
        account_id = str(
            account.get("account_id")
            or wrapper.get("account_id")
            or account.get("id")
            or wrapper.get("id")
            or ""
        ).strip()
        role = str(
            account.get("account_user_role")
            or wrapper.get("account_user_role")
            or wrapper.get("role")
            or ""
        ).strip()
        if not (plan or account_id or role or ids):
            return
        key = (account_id, plan, role, ",".join(sorted(ids)))
        if key in seen:
            return
        seen.add(key)
        contexts.append(
            {
                "plan_type": plan,
                "chatgpt_account_id": account_id,
                "account_user_role": role,
                "_default": is_default,
                "_ids": ids,
            }
        )

    accounts = data.get("accounts") if isinstance(data, dict) else None
    if isinstance(accounts, dict):
        default = accounts.get("default")
        if isinstance(default, dict):
            add(default, is_default=True)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            add(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(accounts if accounts is not None else data)
    return contexts


def _apply_account_context(
    account: dict[str, Any],
    context: dict[str, str],
    *,
    prefix: str = "",
    overwrite_default: bool = False,
) -> None:
    plan = context.get("plan_type", "")
    account_id = context.get("chatgpt_account_id", "")
    role = context.get("account_user_role", "")

    if plan:
        account[f"{prefix}plan_type"] = plan
        if overwrite_default:
            account["plan_type"] = plan
    if account_id:
        account[f"{prefix}chatgpt_account_id"] = account_id
        if overwrite_default:
            account["chatgpt_account_id"] = account_id
    if role:
        account[f"{prefix}account_user_role"] = role
        if overwrite_default:
            account["account_user_role"] = role


def _active_workspace_id(
    account: dict[str, Any],
    workspace_ids: list[str],
) -> str:
    if not account.get("workspace_membership_active"):
        return ""
    join_results = account.get("join_results")
    if isinstance(join_results, list):
        for result in join_results:
            if not isinstance(result, dict):
                continue
            ws_id = str(result.get("workspace_id") or "").strip()
            if (
                ws_id
                and result.get("ok")
                and result.get("membership_active", True)
                and (not workspace_ids or ws_id in workspace_ids)
            ):
                return ws_id
    return workspace_ids[0] if workspace_ids else ""


def _apply_workspace_export_context(
    account: dict[str, Any],
    workspace_id: str,
    plan_type: str = DEFAULT_WORKSPACE_EXPORT_PLAN,
) -> None:
    if not workspace_id:
        return
    account["workspace_export_status"] = "ok"
    account["workspace_plan_type"] = plan_type
    account["workspace_chatgpt_account_id"] = workspace_id
    account["workspace_account_user_role"] = (
        str(account.get("workspace_account_user_role") or "")
        or str(account.get("account_user_role") or "")
        or "member"
    )
    account["plan_type"] = plan_type
    account["chatgpt_account_id"] = workspace_id
    account["account_user_role"] = account["workspace_account_user_role"]


def _has_verified_workspace_context(account: dict[str, Any]) -> bool:
    return (
        bool(account.get("access_token"))
        and bool(account.get("chatgpt_account_id"))
        and _is_workspace_plan(account.get("plan_type"))
    )


def _verify_workspace_membership(
    session,
    access_token: str,
    workspace_id: str,
) -> tuple[bool, str]:
    resp = session.get(
        f"https://chatgpt.com/backend-api/accounts/{workspace_id}/users",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if resp.status_code == 200:
        return True, ""
    detail = (resp.text or "")[:300] if hasattr(resp, "text") else ""
    return False, detail or f"HTTP {resp.status_code}"


# ── Pipeline stages ─────────────────────────────────────────────────


def run_register(
    config: dict[str, Any],
    accounts_file: Path,
    count: int | None = None,
) -> list[dict[str, Any]]:
    """Stage 1: Register N ChatGPT accounts.

    Returns list of newly registered account records.
    """
    reg_cfg = config.get("registration", {})
    proxy_cfg = config.get("proxy", {})

    total = (
        _positive_int(count, 10)
        if count is not None
        else _positive_int(reg_cfg.get("total", 10), 10)
    )
    threads = _positive_int(reg_cfg.get("threads", 3), 3)
    proxy = str(proxy_cfg.get("url", "")).strip()
    flaresolverr_url = str(proxy_cfg.get("flaresolverr_url", "")).strip()
    mail_cfg = _mail_config_with_proxy(config)

    logger.info(f"Starting registration: {total} accounts, {threads} threads")
    if proxy:
        logger.info(f"Proxy: {proxy}")
    if flaresolverr_url:
        logger.info(f"FlareSolverr: {flaresolverr_url}")

    results: list[dict[str, Any]] = []
    existing = load_accounts(accounts_file)
    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {
            executor.submit(
                register_worker,
                index=i,
                proxy=proxy,
                flaresolverr_url=flaresolverr_url,
                mail_config=mail_cfg,
            ): i
            for i in range(1, total + 1)
        }

        for future in as_completed(futures):
            result = future.result()
            if result["ok"]:
                success_count += 1
                account = result["result"]
                results.append(account)
                existing.append(account)
                save_accounts(accounts_file, existing)
                logger.info(
                    f"[{result['index']}/{total}] ✓ {account['email']} "
                    f"({result.get('cost_seconds', 0):.1f}s)"
                )
            else:
                fail_count += 1
                logger.warning(
                    f"[{result['index']}/{total}] ✗ {result.get('error', 'unknown')}"
                )

    logger.info(
        f"Registration complete: {success_count} success, {fail_count} failed"
    )
    return results


def run_join_workspace(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stage 2: Join each account to the K12 parent workspace.

    Modifies account records in-place with join status.
    """
    ws_cfg = config.get("workspace", {})
    if not ws_cfg.get("enabled", True):
        logger.info("Workspace join disabled — skipping")
        return accounts

    workspace_ids = _string_list(ws_cfg.get("ids", []))
    if not workspace_ids:
        logger.warning("No workspace IDs configured — skipping join")
        return accounts

    route = str(ws_cfg.get("route", "k12_request")).strip() or "k12_request"
    max_retries = _positive_int(ws_cfg.get("max_retries", 3), 3)
    retry_backoff = _positive_int(ws_cfg.get("retry_backoff_ms", 5000), 5000)
    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    threads = _bounded_workers(
        _parallel_threads(config, "join_threads", 3),
        len(accounts),
    )

    logger.info(
        f"Joining {len(accounts)} accounts to {len(workspace_ids)} "
        f"workspace(s), {threads} worker(s)"
    )

    def _join_one(account: dict[str, Any]) -> dict[str, Any]:
        email = account.get("email", "?")
        access_token = account.get("access_token", "")
        if not access_token:
            logger.warning(f"[{email}] No access_token — skipping join")
            account["join_status"] = "skipped"
            return account

        results = join_workspaces(
            access_token=access_token,
            workspace_ids=workspace_ids,
            route=route,
            max_retries=max_retries,
            retry_backoff_ms=retry_backoff,
            proxy=proxy,
        )

        request_ok = all(r["ok"] for r in results)
        membership_ok = request_ok

        if request_ok:
            verify_session = None
            try:
                verify_session = _create_http_session(proxy)
                membership_map: dict[str, tuple[bool, str]] = {}
                for ws_id in workspace_ids:
                    membership_map[ws_id] = _verify_workspace_membership(
                        verify_session,
                        access_token,
                        ws_id,
                    )
                for result in results:
                    active, detail = membership_map.get(
                        str(result.get("workspace_id") or ""),
                        (False, "workspace verification missing"),
                    )
                    result["membership_active"] = active
                    if detail:
                        result["membership_detail"] = detail
                membership_ok = all(active for active, _ in membership_map.values())
            except Exception as e:
                membership_ok = False
                for result in results:
                    result["membership_active"] = False
                    result["membership_detail"] = f"verification error: {e}"
            finally:
                if verify_session:
                    verify_session.close()

        all_ok = request_ok and membership_ok
        account["join_status"] = "ok" if all_ok else "failed"
        account["join_results"] = results
        account["workspace_membership_active"] = membership_ok

        if all_ok:
            logger.info(f"[{email}] ✓ Joined {len(workspace_ids)} workspace(s)")
        else:
            errors = [
                r.get("error")
                or r.get("membership_detail")
                or "workspace membership not active"
                for r in results
                if (not r["ok"]) or not r.get("membership_active", request_ok)
            ]
            logger.warning(f"[{email}] ✗ Join failed: {', '.join(errors)}")

        return account

    if threads == 1 or len(accounts) <= 1:
        for account in accounts:
            _join_one(account)
        return accounts

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_join_one, account) for account in accounts]
        for future in as_completed(futures):
            future.result()

    return accounts


def run_re_login(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Stage 3: Re-login each account with Team space selection.

    Gets team-scoped tokens for accounts that successfully joined.
    NOTE: This step requires browser-based OAuth login flow and is
    currently skipped by default. Use registration tokens directly.
    """
    ws_cfg = config.get("workspace", {})
    re_login_enabled = ws_cfg.get("re_login_enabled", False)

    if not re_login_enabled:
        logger.info("Team re-login disabled — using registration tokens for export")
        for account in accounts:
            account["team_login_status"] = "skipped"
        return accounts

    proxy_cfg = config.get("proxy", {})
    proxy = str(proxy_cfg.get("url", "")).strip()
    flaresolverr_url = str(proxy_cfg.get("flaresolverr_url", "")).strip()
    mail_cfg = _mail_config_with_proxy(config)
    workspace_ids = _string_list(ws_cfg.get("ids", []))
    threads = _bounded_workers(
        _parallel_threads(config, "login_threads", 1),
        len(accounts),
    )

    logger.info(
        f"Re-logging {len(accounts)} accounts for team-scoped tokens, "
        f"{threads} worker(s)"
    )

    def _login_one(account: dict[str, Any]) -> dict[str, Any]:
        email = account.get("email", "")
        password = account.get("password", "")
        join_status = account.get("join_status", "")

        if join_status != "ok":
            logger.info(f"[{email}] Join failed/skipped — skipping re-login")
            account["team_login_status"] = "skipped"
            return account

        if not email or not password:
            logger.warning(f"[{email}] Missing email or password — skipping re-login")
            account["team_login_status"] = "skipped"
            return account

        for key in (
            "team_access_token",
            "team_refresh_token",
            "team_id_token",
            "team_plan_type",
            "team_chatgpt_account_id",
            "team_account_user_role",
            "team_login_error",
        ):
            account.pop(key, None)

        try:
            logger.info(f"[{email}] Starting team re-login")
            team_tokens = re_login_for_team_token(
                email=email,
                password=password,
                mail_config=mail_cfg,
                proxy=proxy,
                flaresolverr_url=flaresolverr_url,
                workspace_id=workspace_ids[0] if workspace_ids else "",
            )

            # Store team-scoped tokens in a separate field
            account["team_access_token"] = team_tokens["access_token"]
            account["team_refresh_token"] = team_tokens["refresh_token"]
            account["team_id_token"] = team_tokens["id_token"]

            check_session = None
            try:
                check_session = _create_http_session(proxy)
                context = _fetch_account_context(
                    check_session,
                    team_tokens["access_token"],
                    workspace_id=workspace_ids[0] if workspace_ids else "",
                )
                _apply_account_context(
                    account,
                    context,
                    prefix="team_",
                    overwrite_default=True,
                )

                if not _is_workspace_plan(context.get("plan_type", "")):
                    raise RuntimeError(
                        f"team token still resolved to personal scope "
                        f"(plan={context.get('plan_type') or 'unknown'})"
                    )
            finally:
                if check_session:
                    check_session.close()

            account["team_login_status"] = "ok"
            logger.info(
                f"[{email}] ✓ Team login successful "
                f"(plan={account.get('team_plan_type', account.get('plan_type', '?'))})"
            )
        except Exception as e:
            logger.warning(f"[{email}] ✗ Team login failed: {e}")
            account["team_login_status"] = "failed"
            account["team_login_error"] = str(e)

        return account

    if threads == 1 or len(accounts) <= 1:
        for account in accounts:
            _login_one(account)
        return accounts

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_login_one, account) for account in accounts]
        for future in as_completed(futures):
            future.result()

    return accounts


def run_refresh_tokens(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Refresh access tokens and enrich with workspace info from check API.

    After joining a workspace, refreshing the token ensures the token
    is valid for the current context.  Then we call /accounts/check
    to get the real plan_type and account_id (the JWT doesn't carry
    workspace claims).
    """
    proxy = str(config.get("proxy", {}).get("url", "")).strip()
    ws_cfg = config.get("workspace", {})
    workspace_ids = _string_list(ws_cfg.get("ids", []))
    workspace_plan = str(
        ws_cfg.get("export_plan_type") or DEFAULT_WORKSPACE_EXPORT_PLAN
    ).strip() or DEFAULT_WORKSPACE_EXPORT_PLAN
    threads = _bounded_workers(
        _parallel_threads(config, "refresh_threads", 3),
        len(accounts),
    )

    logger.info(
        f"Refreshing tokens and checking account info for {len(accounts)} "
        f"accounts, {threads} worker(s)"
    )

    def _refresh_one(account: dict[str, Any]) -> dict[str, Any]:
        email = account.get("email", "")
        rt = account.get("refresh_token", "")

        if not rt:
            logger.warning(f"[{email}] No refresh_token — skipping refresh")
            return account

        session = None
        try:
            session = _create_http_session(proxy)

            # Step 1: Refresh the access token
            resp = session.post(
                "https://auth.openai.com/oauth/token",
                data={
                    "client_id": "app_2SKx67EdpoN0G6j64rFvigXD",
                    "grant_type": "refresh_token",
                    "refresh_token": rt,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_at = data.get("access_token", "")
                new_rt = data.get("refresh_token", "")
                if new_at:
                    account["access_token"] = new_at
                if new_rt:
                    account["refresh_token"] = new_rt
                logger.info(f"[{email}] Token refreshed")
            else:
                logger.warning(f"[{email}] Token refresh failed: HTTP {resp.status_code}")

            # Step 2: Call check API to get real plan_type and account_id
            at = account.get("access_token", "")
            if at:
                active_workspace_id = _active_workspace_id(account, workspace_ids)
                context = _fetch_account_context(
                    session,
                    at,
                    workspace_id=active_workspace_id,
                )
                _apply_account_context(account, context)
                if active_workspace_id:
                    _apply_workspace_export_context(
                        account,
                        active_workspace_id,
                        str(account.get("plan_type") or workspace_plan),
                    )
                logger.info(
                    f"[{email}] Check API: "
                    f"plan={context.get('plan_type', '')} "
                    f"account_id={context.get('chatgpt_account_id', '')[:30] or '?'} "
                    f"role={context.get('account_user_role', '')}"
                )
                if account.get("workspace_export_status") == "ok":
                    logger.info(
                        f"[{email}] Workspace export context: "
                        f"plan={account.get('workspace_plan_type')} "
                        f"account_id={account.get('workspace_chatgpt_account_id')}"
                    )

        except Exception as e:
            logger.warning(f"[{email}] Refresh/check error: {e}")
        finally:
            if session:
                session.close()

        return account

    if threads == 1 or len(accounts) <= 1:
        for account in accounts:
            _refresh_one(account)
        return accounts

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(_refresh_one, account) for account in accounts]
        for future in as_completed(futures):
            future.result()

    return accounts


def run_export(
    config: dict[str, Any],
    accounts: list[dict[str, Any]],
    output_file: Path | None = None,
) -> tuple[str, str]:
    """Stage 4: Export accounts as sub2api JSON.

    Uses team-scoped tokens (team_access_token) when available,
    falls back to personal tokens.
    """
    team_setting = config.get("sub2api", {}).get("require_team_tokens", "auto")
    if isinstance(team_setting, bool):
        require_team = team_setting
    else:
        require_team = bool(config.get("workspace", {}).get("re_login_enabled", False))
    if require_team:
        accounts = [
            account
            for account in accounts
            if (
                account.get("team_login_status") == "ok"
                and account.get("team_access_token")
                and _is_workspace_plan(
                    account.get("team_plan_type") or account.get("plan_type")
                )
            )
            or (
                account.get("workspace_export_status") == "ok"
                and account.get("access_token")
                and account.get("workspace_chatgpt_account_id")
                and _is_workspace_plan(
                    account.get("workspace_plan_type") or account.get("plan_type")
                )
            )
            or _has_verified_workspace_context(account)
        ]
        if not accounts:
            raise RuntimeError(
                "No verified team/workspace-scoped accounts available for export"
            )

    export_accounts = []
    for account in accounts:
        export = dict(account)
        if account.get("team_login_status") == "ok":
            export["access_token"] = account.get("team_access_token", account.get("access_token", ""))
            export["refresh_token"] = account.get("team_refresh_token", account.get("refresh_token", ""))
            export["id_token"] = account.get("team_id_token", account.get("id_token", ""))
            export["plan_type"] = account.get("team_plan_type", account.get("plan_type", ""))
            export["chatgpt_account_id"] = account.get("team_chatgpt_account_id", account.get("chatgpt_account_id", ""))
            export["account_user_role"] = account.get("team_account_user_role", account.get("account_user_role", ""))
            export["source_type"] = "team_relogin"
        elif account.get("workspace_export_status") == "ok":
            export["plan_type"] = account.get("workspace_plan_type", account.get("plan_type", ""))
            export["chatgpt_account_id"] = account.get("workspace_chatgpt_account_id", account.get("chatgpt_account_id", ""))
            export["account_user_role"] = account.get("workspace_account_user_role", account.get("account_user_role", ""))
            export["source_type"] = "workspace_join"
        elif _has_verified_workspace_context(account):
            export["source_type"] = "workspace_check"
        # else: use registration tokens as-is
        export_accounts.append(export)

    output_path = _resolve_export_output_path(config, output_file)

    json_str, actual_path = export_sub2api_json(export_accounts, output_path)
    logger.info(f"Exported {len(export_accounts)} accounts to {actual_path}")
    return json_str, actual_path


# ── Full pipeline ───────────────────────────────────────────────────


def run_full_pipeline(
    config: dict[str, Any],
    count: int | None = None,
    output_file: str | None = None,
    accounts_file: str | None = None,
) -> dict[str, Any]:
    """Run the complete pipeline: register → join → re-login → export.

    Args:
        config: Full config dict from config.yaml
        count: Override registration count
        output_file: Override sub2api output path
        accounts_file: Override accounts storage path

    Returns:
        Summary dict with counts
    """
    config_dir = Path(config.get("_config_dir", "."))
    af = Path(accounts_file) if accounts_file else config_dir / "registered_accounts.json"
    of = Path(output_file) if output_file else None

    logger.info("=" * 60)
    logger.info("Pipeline started: register → join → refresh/check → export")
    logger.info("=" * 60)

    # Stage 1: Register
    new_accounts = run_register(config, af, count=count)
    if not new_accounts:
        logger.error("No accounts registered — pipeline aborted")
        return {
            "registered": 0,
            "joined": 0,
            "refreshed": 0,
            "exported": 0,
            "accounts_file": str(af),
            "output_file": "",
        }

    # Stage 2: Join workspace
    joined_accounts = run_join_workspace(config, new_accounts)
    save_accounts(af, joined_accounts)

    re_login_enabled = config.get("workspace", {}).get("re_login_enabled", False)
    if re_login_enabled:
        # Stage 3a: Explicit team re-login. Only team-token successes are exported.
        refreshed_accounts = run_re_login(config, joined_accounts)
        save_accounts(af, refreshed_accounts)
    else:
        # Stage 3b: Default refresh/check path for personal registration tokens.
        refreshed_accounts = run_refresh_tokens(config, joined_accounts)
        save_accounts(af, refreshed_accounts)

    # Stage 4: Export (uses plan_type and account_id from check API)
    all_accounts = load_accounts(af)
    if re_login_enabled:
        all_accounts = [
            account
            for account in all_accounts
            if account.get("team_login_status") == "ok"
        ]
        if not all_accounts:
            logger.error("No team-scoped tokens obtained — export aborted")
            return {
                "registered": len(new_accounts),
                "joined": sum(
                    1 for a in refreshed_accounts if a.get("join_status") == "ok"
                ),
                "refreshed": 0,
                "exported": 0,
                "accounts_file": str(af),
                "output_file": "",
            }

    try:
        _, actual_output = run_export(config, all_accounts, of)
    except RuntimeError as error:
        logger.error(f"Export aborted: {error}")
        return {
            "registered": len(new_accounts),
            "joined": sum(
                1 for a in refreshed_accounts if a.get("join_status") == "ok"
            ),
            "refreshed": 0,
            "exported": 0,
            "accounts_file": str(af),
            "output_file": "",
        }

    registered = len(new_accounts)
    joined = sum(1 for a in refreshed_accounts if a.get("join_status") == "ok")
    refreshed = (
        sum(1 for a in refreshed_accounts if a.get("team_login_status") == "ok")
        if re_login_enabled
        else sum(
            1 for a in refreshed_accounts
            if _is_workspace_plan(a.get("plan_type"))
        )
    )
    exported = len(all_accounts)

    logger.info("=" * 60)
    logger.info(
        f"Pipeline complete: "
        f"registered={registered}, joined={joined}, "
        f"refreshed={refreshed}, exported={exported}"
    )
    logger.info("=" * 60)

    return {
        "registered": registered,
        "joined": joined,
        "refreshed": refreshed,
        "exported": exported,
        "accounts_file": str(af),
        "output_file": actual_output,
    }
