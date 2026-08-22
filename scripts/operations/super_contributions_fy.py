#!/usr/bin/env python3
"""Per-financial-year super contributions from the PocketSmith REST fund feed.

Read-only. The REST Industry Super transaction account IS the fund feed: employer
SG, voluntary (personal deductible) contributions, contribution tax, fees and
insurance premiums, and investment earnings all arrive as transactions on it.

Australian FY runs 1 Jul to 30 Jun; `fy` is the calendar year the FY ends in.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.core.api_client import PocketSmithClient  # noqa: E402


USER_ID = 362812
REST_ACCOUNT_ID = 2141959
REST_ACCOUNT_NAME = "REST Industry Super"
# 5 FYs of carry-forward horizon plus the current one.
DEFAULT_SINCE = "2020-07-01"

EMPLOYER_SG = "employerSg"
PERSONAL_DEDUCTIBLE = "personalDeductible"
CONTRIBUTION_TAX = "contributionTax"
FEES_AND_INSURANCE = "feesAndInsurance"
EARNINGS = "earnings"
# First Home Super Saver release: money withdrawn for the house deposit. Not a
# contribution and not a fee; it does not change cap history (those dollars were
# capped in the FY they went in).
FHSS_RELEASE = "fhssRelease"
OTHER = "other"

BUCKETS = (EMPLOYER_SG, PERSONAL_DEDUCTIBLE, CONTRIBUTION_TAX, FEES_AND_INSURANCE, EARNINGS, FHSS_RELEASE, OTHER)
# Buckets that are contributions into the fund, and their contributionHistory type.
CONTRIBUTION_TYPE = {EMPLOYER_SG: "employer_sg", PERSONAL_DEDUCTIBLE: "personal_deductible"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=DEFAULT_SINCE, help=f"window start (default {DEFAULT_SINCE})")
    parser.add_argument("--today", default=date.today().isoformat(), help="window end (default today)")
    parser.add_argument("--json", action="store_true", help="accepted for parity; output is always JSON")
    return parser.parse_args()


def financial_year(day: date) -> int:
    """Calendar year the Australian FY containing `day` ends in."""
    return day.year + 1 if day.month >= 7 else day.year


def fy_label(fy: int) -> str:
    return f"FY{fy - 1}-{fy % 100:02d}"


def classify(payee: str, category: str, amount: float) -> str:
    """Bucket one REST-account row. Payee wording changed over the years (`Admin
    Fee (%)` → `Administration Fee`, `Contributions Tax` → `Contribution tax`,
    `TAL Std Death Premium` → `Insurance premium: Standard Death cover`), so match
    on the stable words. Anything unmatched lands in `other` and is warned about
    rather than silently dropped.

    Tax first: `Contributions Tax` also matches the contribution patterns."""

    text = payee.lower()
    if ("tax" in text and "cont" in text) or "transfer in tax" in text:
        return CONTRIBUTION_TAX
    if "fee" in text or "premium" in text:
        return FEES_AND_INSURANCE
    if "investment earnings" in text:
        return EARNINGS
    if text.strip() == "claim" and amount < 0 and "transfer" in category.lower():
        return FHSS_RELEASE
    # `Member Cont` is Bailey's own concessional contribution, not employer SG.
    if "voluntary contribution" in text or "member cont" in text:
        return PERSONAL_DEDUCTIBLE
    if "trustee for de groot" in text or "sg voucher" in text:
        return EMPLOYER_SG
    if "super contribution" in category.lower() and amount > 0:
        return EMPLOYER_SG
    return OTHER


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Bucket + FY-aggregate already-normalized rows ({date, payee, category, amount})."""

    by_fy: dict[int, dict[str, float]] = defaultdict(lambda: dict.fromkeys(BUCKETS, 0.0))
    last_received: dict[int, tuple[str, float]] = {}
    contributions: list[dict[str, Any]] = []
    warnings: list[str] = []

    for row in sorted(rows, key=lambda item: str(item.get("date") or "")):
        try:
            day = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            warnings.append(f"skipped row with unparseable date: {row!r}")
            continue
        amount = float(row.get("amount") or 0.0)
        payee = str(row.get("payee") or "")
        bucket = classify(payee, str(row.get("category") or ""), amount)
        fy = financial_year(day)
        by_fy[fy][bucket] += amount

        if bucket == OTHER:
            warnings.append(f"unclassified {day.isoformat()} {payee} {amount:+.2f}")
        elif bucket in CONTRIBUTION_TYPE and amount > 0:
            contributions.append(
                {
                    "dateReceived": day.isoformat(),
                    "amount": round(amount, 2),
                    "fund": "REST",
                    "type": CONTRIBUTION_TYPE[bucket],
                    "source": "PocketSmith",
                }
            )
            last_received[fy] = (day.isoformat(), round(amount, 2))

    rows_by_fy = []
    for fy in sorted(by_fy):
        totals = {key: round(value, 2) for key, value in by_fy[fy].items()}
        received_date, received_amount = last_received.get(fy, (None, None))
        rows_by_fy.append(
            {
                "fy": fy,
                "fyLabel": fy_label(fy),
                **totals,
                "netIntoFund": round(
                    totals[EMPLOYER_SG] + totals[PERSONAL_DEDUCTIBLE] + totals[CONTRIBUTION_TAX], 2
                ),
                "lastReceivedDate": received_date,
                "lastReceivedAmount": received_amount,
            }
        )
    return {"byFy": rows_by_fy, "contributions": contributions, "warnings": warnings}


def find_rest_account(client: PocketSmithClient) -> dict[str, Any]:
    """The pinned account id wins; a name match is only the fallback if the id is
    gone (a bare "rest" substring would also match "Interest")."""
    accounts = client.get_transaction_accounts(USER_ID)
    for account in accounts:
        if account.get("id") == REST_ACCOUNT_ID:
            return {"id": REST_ACCOUNT_ID, "name": account.get("name") or REST_ACCOUNT_NAME}
    for account in accounts:
        if "rest industry super" in str(account.get("name") or "").lower():
            return {"id": account["id"], "name": account["name"]}
    return {"id": REST_ACCOUNT_ID, "name": REST_ACCOUNT_NAME}


def fetch_rows(client: PocketSmithClient, account_id: int, since: str, today: str) -> list[dict[str, Any]]:
    """All transactions on one account. start_date without end_date is a 400, so
    both are always sent; page until a short page arrives."""

    per_page = 100
    rows: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = client.get(
            f"/transaction_accounts/{account_id}/transactions",
            params={"start_date": since, "end_date": today, "per_page": per_page, "page": page},
        )
        rows.extend(
            {
                "date": txn.get("date"),
                "payee": txn.get("payee") or txn.get("original_payee") or "",
                "category": (txn.get("category") or {}).get("title") if isinstance(txn.get("category"), dict) else "",
                "amount": txn.get("amount"),
            }
            for txn in batch
        )
        if len(batch) < per_page:
            return rows
        page += 1


def main() -> None:
    args = parse_args()
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    client = PocketSmithClient()
    account = find_rest_account(client)
    rows = fetch_rows(client, int(account["id"]), args.since, args.today)
    result = {"account": account, "asOf": args.today, **summarise(rows)}
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
