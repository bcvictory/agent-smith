#!/usr/bin/env python3
"""Sync the REST Industry Super member portal into PocketSmith.

The REST member portal (member.rest.com.au) is a React SPA over the Link Group
MCX API (api-prd1.linkgroup.com). This script:

1. Harvests an Okta access token from a persistent Chrome profile
   (~/.agent-chrome-profile) via agent-browser. Login is manual (SMS MFA); later
   runs ride the portal's own silent-auth renewal while the Okta session lives.
2. Pulls the live balance (header endpoint) and the transaction ledger (export
   endpoint) directly from the API with the Bearer token.
3. Imports genuinely new rows into the PocketSmith REST account
   (transaction_account 2141959) using match-and-skip dedupe: rows are paired
   per (date, amount) group with payee-similarity tie-breaks, so the existing
   hand-entered / CSV-imported register is never duplicated.
4. Resets the account's starting-balance anchor to the portal balance as of
   today. PocketSmith counts only transactions dated strictly AFTER the anchor
   date (verified 2026-09-01), so the register can hold the full ledger while
   the displayed balance always equals the portal exactly — no fake
   true-up transactions.

Dry-run by default; pass --apply to write. Exit codes: 0 ok, 1 error, 2 auth
needed (an email is sent unless --no-email).

Required in .env (this repo is public, so none of these are hardcoded):
  REST_MEMBER_NUMBER          member number from the portal
  REST_APIM_SUBSCRIPTION_KEY  ocp-apim-subscription-key from the portal JS bundle
  REST_SYNC_EMAIL             address for auth-failure notices (optional)

Usage:
  uv run python -u scripts/operations/rest_super_sync.py            # dry-run
  uv run python -u scripts/operations/rest_super_sync.py --apply
  uv run python -u scripts/operations/rest_super_sync.py --full     # since 2023-07-01
  uv run python -u scripts/operations/rest_super_sync.py --apply --opportunistic

--opportunistic is the hourly launchd mode. The portal issues a 60-minute token and
its Okta client is authorization_code only (no refresh token, confirmed 2026-09-20),
so a session lasts hours while a weekly cron needs it alive at one exact minute: the
old Sunday 07:30 job failed its first 3 runs for exactly that reason. This mode
instead rides whatever session Bailey already has open, and says nothing when there
isn't one.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import date, timedelta
from difflib import SequenceMatcher
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.core.api_client import PocketSmithClient  # noqa: E402

# Load before the constants below read the environment.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# --- REST portal / Link MCX constants (from HAR recon 2026-09-01) ---
REST_API = "https://api-prd1.linkgroup.com/rss/mcx/web"
PLAN_CODE = "RS"
# Member number and the portal's static APIM key live in .env — this repo is public.
MEMBER_NUMBER = os.environ.get("REST_MEMBER_NUMBER", "")
APIM_SUBSCRIPTION_KEY = os.environ.get("REST_APIM_SUBSCRIPTION_KEY", "")
CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
PORTAL_ORIGIN = "https://member.rest.com.au"
DASHBOARD_URL = f"{PORTAL_ORIGIN}/accumulation/dashboard"

# --- PocketSmith constants ---
PS_ACCOUNT_ID = 2141959  # REST Industry Super transaction account
CATEGORY_BY_GROUP = {
    "*CTR": 12835870,    # Super Contribution (employer SG)
    "*TFI": 12835870,    # Super Contribution (voluntary / transfer in)
    "*CTRTAX": 12835870, # Super Contribution (contribution tax, negative)
    "*FEE": 12958744,    # Investments
    "*INT": 12958744,    # Investments (annual investment earnings)
    "*INS": 12689329,    # Insurance
    "*CLM": 12689236,    # Transfers (FHSSS release etc.)
}
SYNC_LABEL = "rest-super-sync"

# --- Local environment ---
PROFILE_DIR = Path.home() / ".agent-chrome-profile"
CDP_PORT = 9223
STATE_FILE = Path.home() / "Pocketsmith" / "logs" / "rest-super-sync-state.json"
FULL_HISTORY_SINCE = "2023-07-01"  # earliest date the fund's export exposes
DEFAULT_WINDOW_DAYS = 45

EMAIL_TO = os.environ.get("REST_SYNC_EMAIL", "")
EMAIL_FROM = EMAIL_TO


# ---------------------------------------------------------------------------
# Browser / token harvest
# ---------------------------------------------------------------------------

SESSION = "rest-sync"


def _run_agent_browser(*args: str, timeout: int = 60) -> tuple[bool, str]:
    """Run one agent-browser CLI call. Returns (ok, stdout)."""
    try:
        cp = subprocess.run(
            ["agent-browser", "--session", SESSION, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return False, str(exc)
    out = (cp.stdout or "") + (cp.stderr or "")
    return cp.returncode == 0 and "✗" not in out, out


def _agent_browser_json(*args: str) -> dict[str, Any]:
    """Run an agent-browser call with --json; returns its `data` payload ({} on failure).

    Human-readable output is not a stable contract: a 2026-09 CLI upgrade renamed tab
    ids (`[4]` → `[t4]`) and prefixed storage values with `<key>: `, breaking both
    text parsers at once.
    """
    ok, out = _run_agent_browser(*args, "--json")
    if not ok:
        return {}
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return {}
    if not payload.get("success"):
        return {}
    return payload.get("data") or {}


def parse_tab_list(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Pull (tabId, url) pairs out of `agent-browser tab list --json` data."""
    return [
        (str(tab.get("tabId")), str(tab.get("url") or ""))
        for tab in (payload.get("tabs") or [])
        if tab.get("tabId")
    ]


def token_from_okta_storage(raw: str) -> tuple[str | None, int]:
    """Extract (access_token, expiresAt) from okta-token-storage JSON."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, 0
    access = payload.get("accessToken") or {}
    token = access.get("accessToken")
    expires = int(access.get("expiresAt") or 0)
    if not token:
        return None, 0
    return str(token), expires


def _launch_profile_chrome() -> None:
    """Open the dashboard in the persistent profile via real Chrome, not Playwright.

    Playwright `page.goto` against member.rest.com.au fails with
    net::ERR_HTTP2_PROTOCOL_ERROR; Chrome itself loads the portal fine.

    --restore-last-session keeps the portal's session cookies (__Host-Http-Session,
    ASP.NET_SessionId) across a Chrome restart; without it they die with the process
    and every restart costs a fresh SMS login.
    """
    subprocess.run(
        [
            "open", "-na", "Google Chrome", "--args",
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run",
            "--restore-last-session",
            DASHBOARD_URL,
        ],
        capture_output=True, timeout=30,
    )


def _connect_cdp() -> bool:
    ok, _ = _run_agent_browser("connect", str(CDP_PORT))
    return ok


def _focus_portal_tab() -> bool:
    """Switch to a member.rest.com.au tab. Always reconnect first — a leftover
    session can be stuck on chrome-error:// from a failed Playwright goto."""
    if not _connect_cdp():
        _launch_profile_chrome()
        for _ in range(10):
            time.sleep(2)
            if _connect_cdp():
                break
        else:
            return False
    if _select_portal_tab():
        return True
    _launch_profile_chrome()
    time.sleep(4)
    _connect_cdp()
    return _select_portal_tab()


def _select_portal_tab() -> bool:
    """Activate the first member.rest.com.au tab, if one is open."""
    for tab_id, url in parse_tab_list(_agent_browser_json("tab", "list")):
        if "member.rest.com.au" in url:
            _run_agent_browser("tab", tab_id)
            return True
    return False


def _read_okta_storage() -> str:
    data = _agent_browser_json("storage", "local", "get", "okta-token-storage")
    return str(data.get("value") or "").strip()


def peek_token() -> str | None:
    """Return a live access token, or None — WITHOUT ever launching Chrome.

    The opportunistic probe runs hourly, so it must be silent and side-effect free:
    no Chrome window appearing on Bailey's desktop, no navigation that could disturb
    a live portal session. If Chrome is not already up with a portal tab, the answer
    is simply "no token right now".
    """
    if not _connect_cdp() or not _select_portal_tab():
        return None
    token, expires_at = token_from_okta_storage(_read_okta_storage())
    return token if token and expires_at > int(time.time()) + 60 else None


def harvest_token() -> tuple[str | None, str]:
    """Return (access_token, status). status: ok | renewed | auth_needed | error."""
    if not _focus_portal_tab():
        return None, "error"
    for attempt in range(3):
        token, expires_at = token_from_okta_storage(_read_okta_storage())
        if token and expires_at > int(time.time()) + 60:
            return token, "ok" if attempt == 0 else "renewed"
        # Missing / near-expiry: ask Chrome (not Playwright) to reload the
        # dashboard so the portal's silent auth can mint a new token.
        _launch_profile_chrome()
        time.sleep(6)
        _connect_cdp()
        _focus_portal_tab()
    return None, "auth_needed"


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

def rest_get(path: str, token: str, params: dict[str, Any]) -> Any:
    # Akamai on api-prd1.linkgroup.com 403s the default Python-requests UA
    # ("Access Denied" HTML). The portal's Chrome UA is enough.
    response = requests.get(
        f"{REST_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "ocp-apim-subscription-key": APIM_SUBSCRIPTION_KEY,
            "Origin": PORTAL_ORIGIN,
            "Referer": f"{PORTAL_ORIGIN}/",
            "Accept": "application/json",
            "Accept-Language": "en-AU,en;q=0.9",
            "User-Agent": CHROME_UA,
            "x-correlation-id": str(uuid.uuid4()),
            "sec-ch-ua": '"Chromium";v="152", "Google Chrome";v="152", "Not:A-Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "cross-site",
        },
        params=params,
        timeout=60,
    )
    if response.status_code in (401, 403):
        raise PermissionError(f"REST API {response.status_code} on {path}")
    response.raise_for_status()
    return response.json()


def fetch_portal(token: str, since: str, today: str) -> dict[str, Any]:
    member = f"/plans/{PLAN_CODE}/members/{MEMBER_NUMBER}"
    header = rest_get(f"/dashboard{member}/header", token, {"includeInterest": "true"})
    export = rest_get(
        f"/superannuation{member}/export",
        token,
        {
            "FromDate": since,
            "ToDate": today,
            "ExclusionAccountTypes": "",
            "zeroTransactionCodesToBeIncluded": "",
            "TransactionSubCodes": "",
        },
    )
    return {"header": header, "export": export.get("transactionExport") or []}


def normalize_export_row(row: dict[str, Any]) -> dict[str, Any]:
    total = round(sum(float(a.get("amount") or 0.0) for a in row.get("accounts") or []), 2)
    return {
        "rest_id": row.get("transactionId"),
        "type": row.get("transactionType") or "",
        "group": row.get("transactionGroupCode") or "",
        "date": str(row.get("date") or "")[:10],
        "payee": row.get("description") or "",
        "amount": total,
    }


# ---------------------------------------------------------------------------
# Dedupe: multiset match per (date, amount) group, payee-similarity pairing
# ---------------------------------------------------------------------------

def _norm_payee(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm_payee(a), _norm_payee(b)).ratio()


def reconcile(
    export_rows: list[dict[str, Any]], ps_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Pair export rows with PocketSmith rows.

    Returns (matched, unmatched_export, unmatched_ps). Payee wording drift on a
    matched pair is noted on the match as `wording_drift` for the report.
    """
    groups: dict[tuple[str, float], dict[str, list]] = {}
    for row in export_rows:
        groups.setdefault((row["date"], round(float(row["amount"]), 2)), {}).setdefault("export", []).append(row)
    for row in ps_rows:
        groups.setdefault((row["date"], round(float(row["amount"]), 2)), {}).setdefault("ps", []).append(row)

    matched: list[dict[str, Any]] = []
    unmatched_export: list[dict[str, Any]] = []
    unmatched_ps: list[dict[str, Any]] = []

    for (_day, _amount), group in sorted(groups.items()):
        exps = list(group.get("export", []))
        pss = list(group.get("ps", []))
        # Greedy best-similarity pairing within the group.
        while exps and pss:
            best = (0.0, 0, 0)
            for ei, exp in enumerate(exps):
                for pi, ps in enumerate(pss):
                    score = _similarity(str(exp.get("payee") or ""), str(ps.get("payee") or ""))
                    if score > best[0]:
                        best = (score, ei, pi)
            score, ei, pi = best
            exp = exps.pop(ei)
            ps = pss.pop(pi)
            pair = {"export": exp, "ps": ps}
            if score < 0.99:
                pair["wording_drift"] = {"export_payee": exp.get("payee"), "ps_payee": ps.get("payee"), "similarity": round(score, 3)}
            matched.append(pair)
        unmatched_export.extend(exps)
        unmatched_ps.extend(pss)
    return matched, unmatched_export, unmatched_ps


# ---------------------------------------------------------------------------
# PocketSmith
# ---------------------------------------------------------------------------

def fetch_ps_rows(client: PocketSmithClient, since: str, today: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.get(
            f"/transaction_accounts/{PS_ACCOUNT_ID}/transactions",
            params={"start_date": since, "end_date": today, "per_page": 100, "page": page},
        )
        rows.extend(
            {
                "id": txn.get("id"),
                "date": str(txn.get("date") or "")[:10],
                "payee": txn.get("payee") or "",
                "amount": txn.get("amount"),
                "category": (txn.get("category") or {}).get("title") if isinstance(txn.get("category"), dict) else "",
            }
            for txn in batch
        )
        if len(batch) < 100:
            return rows
        page += 1


def import_row(client: PocketSmithClient, row: dict[str, Any], apply: bool) -> dict[str, Any]:
    category_id = CATEGORY_BY_GROUP.get(row["group"])
    payload: dict[str, Any] = {
        "payee": row["payee"],
        "amount": row["amount"],
        "date": row["date"],
        "labels": [SYNC_LABEL],
        "note": f"rest-txn:{row['rest_id']} {row['type']}".strip(),
    }
    if category_id:
        payload["category_id"] = category_id
    if not apply:
        return {"dry_run": True, "payload": payload}
    created = client.post(f"/transaction_accounts/{PS_ACCOUNT_ID}/transactions", payload)
    return {"id": created.get("id"), "payload": payload}


def reset_anchor(client: PocketSmithClient, portal_balance: float, today: str, apply: bool) -> dict[str, Any]:
    current = client.get(f"/transaction_accounts/{PS_ACCOUNT_ID}")
    before = {
        "starting_balance": current.get("starting_balance"),
        "starting_balance_date": current.get("starting_balance_date"),
        "current_balance": current.get("current_balance"),
    }
    drift = round(portal_balance - float(before["current_balance"] or 0.0), 2)
    result: dict[str, Any] = {"before": before, "portal_balance": portal_balance, "drift": drift, "changed": False}
    if str(before["starting_balance_date"]) >= today or abs(drift) < 1.00:
        return result
    payload = {"starting_balance": round(portal_balance, 2), "starting_balance_date": today}
    if apply:
        client.put(f"/transaction_accounts/{PS_ACCOUNT_ID}", payload)
        after = client.get(f"/transaction_accounts/{PS_ACCOUNT_ID}")
        result["after"] = {
            "starting_balance": after.get("starting_balance"),
            "starting_balance_date": after.get("starting_balance_date"),
            "current_balance": after.get("current_balance"),
        }
        result["changed"] = True
    else:
        result["after"] = {"starting_balance": payload["starting_balance"], "starting_balance_date": today}
        result["changed"] = "dry-run"
    return result


# ---------------------------------------------------------------------------
# State + notification
# ---------------------------------------------------------------------------

def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def notify_auth_needed(state: dict[str, Any], no_email: bool) -> None:
    last = (state.get("auth") or {}).get("last_email_date")
    today = date.today().isoformat()
    if no_email or last == today or not EMAIL_TO:
        return
    body = (
        "The REST portal login session has ended, so the super sync is paused.\n\n"
        "The portal issues a 60-minute token with no refresh token, so this is normal "
        "and expected; the sync resumes by itself once you are logged in again.\n\n"
        "To re-auth: in the agent Chrome window (profile ~/.agent-chrome-profile), "
        "log in at https://member.rest.com.au/ (member number + SMS code) and leave the "
        "dashboard tab open. The hourly probe picks it up from there, or run it now:\n\n"
        "  cd ~/Pocketsmith/agent-smith && uv run python -u scripts/operations/rest_super_sync.py --apply\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = "REST super sync: re-login needed"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    cp = subprocess.run(
        ["gws", "gmail", "users", "messages", "send",
         "--params", json.dumps({"userId": "me"}), "--json", json.dumps({"raw": raw})],
        capture_output=True, text=True,
    )
    state.setdefault("auth", {})["last_email_date"] = today
    if cp.returncode != 0:
        print(f"[warn] auth-notification email failed: {cp.stderr[:300]}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to PocketSmith (default: dry-run)")
    parser.add_argument("--full", action="store_true", help=f"export from {FULL_HISTORY_SINCE} (reconciliation run)")
    parser.add_argument("--since", help="explicit export FromDate (YYYY-MM-DD), overrides --full/window")
    parser.add_argument("--today", default=date.today().isoformat(), help="window end (default today)")
    parser.add_argument("--no-email", action="store_true", help="suppress the auth-failure email")
    parser.add_argument(
        "--opportunistic", action="store_true",
        help="hourly probe: sync only if a token is already live and a sync is due, else exit 0 quietly",
    )
    parser.add_argument(
        "--min-interval-days", type=int, default=3,
        help="in --opportunistic mode, skip if the last apply is newer than this (default 3)",
    )
    return parser.parse_args()


def probe_line(probe: dict[str, Any]) -> str:
    """One line per quiet hourly probe — a full JSON block 24x a day buries the real runs."""
    stamp = time.strftime("%Y-%m-%d %H:%M")
    return f"[{stamp}] probe: {probe.get('action', 'unknown')}"


def days_since(iso_date: str | None) -> float:
    """Whole days between iso_date and today; a huge number when never/unparseable."""
    if not iso_date:
        return 1e6
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except ValueError:
        return 1e6


def main() -> int:
    args = parse_args()
    missing = [
        name for name, value in (
            ("REST_MEMBER_NUMBER", MEMBER_NUMBER),
            ("REST_APIM_SUBSCRIPTION_KEY", APIM_SUBSCRIPTION_KEY),
        ) if not value
    ]
    if missing:
        print(json.dumps({"error": f"missing in .env: {', '.join(missing)}"}, indent=2))
        return 1
    state = load_state()

    if args.since:
        since = args.since
    elif args.full:
        since = FULL_HISTORY_SINCE
    else:
        since = (date.fromisoformat(args.today) - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()

    report: dict[str, Any] = {
        "mode": "apply" if args.apply else "dry-run",
        "window": {"since": since, "today": args.today},
    }

    auth_state = state.setdefault("auth", {})

    if args.opportunistic:
        # Passive: never launches Chrome, never forces a login. A dead session is the
        # normal resting state, so it exits 0 — launchd should not see hourly failures.
        token = peek_token()
        was_alive = bool(auth_state.get("session_alive"))
        auth_state["session_alive"] = token is not None
        auth_state["last_probe_date"] = date.today().isoformat()
        probe: dict[str, Any] = {"session_alive": token is not None, "was_alive": was_alive}
        report["probe"] = probe

        if token is None:
            # Email only on the alive -> dead edge, so Bailey hears about it the day it
            # happens instead of a week later, and hears about it once.
            if was_alive:
                notify_auth_needed(state, args.no_email)
            probe["action"] = "skipped: no live session"
            save_state(state)
            print(probe_line(probe))
            return 0

        stale_days = days_since(state.get("last_apply"))
        if stale_days < args.min_interval_days:
            probe["action"] = f"skipped: last apply {stale_days:.0f}d ago"
            save_state(state)
            print(probe_line(probe))
            return 0
        probe["action"] = f"syncing: last apply {stale_days:.0f}d ago"
        auth_status = "ok"
    else:
        token, auth_status = harvest_token()

    report["auth"] = {"status": auth_status}
    auth_state["last_run_date"] = date.today().isoformat()
    auth_state["last_status"] = auth_status
    auth_state["session_alive"] = token is not None
    if not token:
        report["error"] = "could not obtain a REST access token; manual login required"
        save_state(state)
        print(json.dumps(report, indent=2, sort_keys=True))
        notify_auth_needed(state, args.no_email)
        save_state(state)
        return 2
    auth_state["last_ok_date"] = date.today().isoformat()

    try:
        portal = fetch_portal(token, since, args.today)
    except PermissionError as exc:
        report["error"] = str(exc)
        save_state(state)
        print(json.dumps(report, indent=2, sort_keys=True))
        notify_auth_needed(state, args.no_email)
        save_state(state)
        return 2

    portal_balance = float((portal["header"] or {}).get("accountBalance") or 0.0)
    export_rows = [
        row
        for row in (normalize_export_row(row) for row in portal["export"])
        if row["date"] and row["payee"]
    ]

    client = PocketSmithClient()
    ps_rows = fetch_ps_rows(client, since, args.today)

    matched, unmatched_export, unmatched_ps = reconcile(export_rows, ps_rows)
    drift_pairs = [m["wording_drift"] for m in matched if "wording_drift" in m]
    drift_counts: dict[tuple[str, str], int] = {}
    for item in drift_pairs:
        key = (str(item["export_payee"]), str(item["ps_payee"]))
        drift_counts[key] = drift_counts.get(key, 0) + 1
    report["reconcile"] = {
        "export_rows": len(export_rows),
        "ps_rows": len(ps_rows),
        "matched": len(matched),
        "wording_drift_count": len(drift_pairs),
        "wording_drift": [
            {"export_payee": a, "ps_payee": b, "count": n}
            for (a, b), n in sorted(drift_counts.items(), key=lambda item: -item[1])
        ],
        "unmatched_ps": [
            {"date": r["date"], "payee": r["payee"], "amount": r["amount"], "category": r["category"]}
            for r in unmatched_ps
        ],
        "unmatched_export": [
            {"date": r["date"], "payee": r["payee"], "amount": r["amount"], "rest_id": r["rest_id"]}
            for r in unmatched_export
        ],
    }

    imported = []
    skipped_uncategorised = []
    for row in sorted(unmatched_export, key=lambda r: (r["date"], r["payee"])):
        if row["group"] not in CATEGORY_BY_GROUP:
            skipped_uncategorised.append(row)
        imported.append(import_row(client, row, args.apply))
    report["imported"] = imported
    if skipped_uncategorised:
        report["warnings"] = [f"no category mapping for group {r['group']!r} ({r['date']} {r['payee']} {r['amount']:+})" for r in skipped_uncategorised]

    report["anchor"] = reset_anchor(client, portal_balance, args.today, args.apply)

    if args.apply:
        seen = set(state.get("seen_keys") or [])
        for row in unmatched_export:
            seen.add(f"{row['rest_id']}|{row['type']}|{row['date']}|{row['amount']}")
        state["seen_keys"] = sorted(seen)
        state["last_apply"] = date.today().isoformat()
        state.setdefault("anchor_history", []).append(
            {"date": date.today().isoformat(), "portal_balance": portal_balance, "anchor": report["anchor"].get("after")}
        )
    save_state(state)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
