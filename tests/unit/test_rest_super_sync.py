"""Tests for the REST portal -> PocketSmith sync (rest_super_sync)."""

import json

from scripts.operations.rest_super_sync import (
    CATEGORY_BY_GROUP,
    SYNC_LABEL,
    import_row,
    normalize_export_row,
    parse_tab_list,
    reconcile,
    reset_anchor,
    token_from_okta_storage,
)


class FakeClient:
    """Records calls; serves canned GET responses keyed by call order."""

    def __init__(self, get_responses=None):
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.puts = []

    def get(self, endpoint, params=None):
        return self.get_responses.pop(0) if self.get_responses else {}

    def post(self, endpoint, data):
        self.posts.append((endpoint, data))
        return {"id": 999000 + len(self.posts)}

    def put(self, endpoint, data):
        self.puts.append((endpoint, data))
        return {}


def test_parse_tab_list_picks_portal_tab():
    payload = {
        "tabs": [
            {"tabId": "t4", "url": "chrome-error://chromewebdata/"},
            {"tabId": "t5", "url": "https://member.rest.com.au/accumulation/dashboard"},
        ]
    }
    tabs = parse_tab_list(payload)
    assert tabs[0] == ("t4", "chrome-error://chromewebdata/")
    assert tabs[1] == ("t5", "https://member.rest.com.au/accumulation/dashboard")
    assert parse_tab_list({}) == []


def test_token_from_okta_storage_reads_nested_access_token():
    raw = json.dumps({"accessToken": {"accessToken": "tok_abc", "expiresAt": 1788237578}})
    token, expires = token_from_okta_storage(raw)
    assert token == "tok_abc"
    assert expires == 1788237578
    assert token_from_okta_storage("not-json") == (None, 0)


def export_row(date="2026-08-31", description="De Groot Business Services Pty Ltd Contribution",
               amount=346.15, txn_id=4230392048, txn_type="*CTR", group="*CTR"):
    return {
        "date": f"{date}T00:00:00+00:00",
        "description": description,
        "paymentPeriod": None,
        "transactionId": txn_id,
        "transactionType": txn_type,
        "transactionGroupCode": group,
        "transactionSubCode": None,
        "accounts": [
            {"id": "EMPLOYER", "description": "Employer ", "amount": amount},
            {"id": "EMP_ADD_SAL_SAC", "description": "Employer Additional", "amount": 0.0},
            {"id": "SALARY_SACRIFICE", "description": "Salary Sacrifice ", "amount": 0.0},
            {"id": "MEMBER", "description": "Member", "amount": 0.0},
        ],
    }


def ps_row(date="2026-08-31", payee="De Groot Business Services Pty Ltd Contribution",
           amount=346.15, category="Super Contribution"):
    return {"id": 111, "date": date, "payee": payee, "amount": amount, "category": category}


# --- normalize_export_row ---

def test_normalize_sums_account_splits_and_rounds_float_noise():
    row = export_row(txn_type="*CTRTAX-*CTRTAX", group="*CTRTAX")
    row["description"] = "Contribution tax"
    row["accounts"] = [{"id": "EMPLOYER", "amount": -564.8299999999999}]
    norm = normalize_export_row(row)
    assert norm == {
        "rest_id": 4230392048,
        "type": "*CTRTAX-*CTRTAX",
        "group": "*CTRTAX",
        "date": "2026-08-31",
        "payee": "Contribution tax",
        "amount": -564.83,
    }


def test_normalize_personal_contribution_reads_member_split():
    row = export_row(description="Voluntary contribution", txn_type="*TFI-MEMBER", group="*TFI")
    row["accounts"] = [
        {"id": "MEMBER", "amount": 14500.0},
        {"id": "EMPLOYER", "amount": 0.0},
    ]
    assert normalize_export_row(row)["amount"] == 14500.0


# --- reconcile ---

def test_reconcile_matches_identical_register():
    matched, unmatched_export, unmatched_ps = reconcile(
        [normalize_export_row(export_row())], [ps_row()]
    )
    assert len(matched) == 1
    assert unmatched_export == []
    assert unmatched_ps == []
    assert "wording_drift" not in matched[0]


def test_reconcile_pairs_fhss_wording_drift_instead_of_duplicating():
    """Export says 'Withdrawal', the hand-entered register says 'Claim -51,996.00'.
    Same date + amount -> same transaction; must not import a duplicate."""
    exp = normalize_export_row(export_row(date="2025-09-05", description="Withdrawal",
                                          amount=-51996.0, txn_type="*CLM", group="*CLM"))
    ps = ps_row(date="2025-09-05", payee="Claim -51,996.00", amount=-51996.0, category="Transfers")
    matched, unmatched_export, unmatched_ps = reconcile([exp], [ps])
    assert unmatched_export == []
    assert unmatched_ps == []
    assert matched[0]["wording_drift"]["export_payee"] == "Withdrawal"


def test_reconcile_multiset_within_group():
    """Two identical export rows on one date, only one in PocketSmith: one pairs,
    one is genuinely new."""
    exp = normalize_export_row(export_row())
    dup = normalize_export_row(export_row(txn_id=4230392049))
    matched, unmatched_export, unmatched_ps = reconcile([exp, dup], [ps_row()])
    assert len(matched) == 1
    assert len(unmatched_export) == 1
    assert unmatched_export[0]["rest_id"] == 4230392049
    assert unmatched_ps == []


def test_reconcile_reports_ps_rows_missing_from_export():
    matched, unmatched_export, unmatched_ps = reconcile([], [ps_row()])
    assert matched == []
    assert unmatched_export == []
    assert unmatched_ps[0]["payee"] == ps_row()["payee"]


# --- import_row ---

def test_import_row_payload_shape_and_category_map():
    client = FakeClient()
    row = normalize_export_row(export_row(txn_type="*INS-STDDO_RTAL", group="*INS",
                                          description="Insurance premium: Standard Death cover",
                                          amount=-2.4))
    result = import_row(client, row, apply=True)
    assert result["id"] == 999001
    endpoint, payload = client.posts[0]
    assert endpoint == "/transaction_accounts/2141959/transactions"
    assert payload["category_id"] == 12689329  # Insurance
    assert payload["labels"] == [SYNC_LABEL]
    assert payload["note"] == "rest-txn:4230392048 *INS-STDDO_RTAL"
    assert payload["amount"] == -2.4


def test_import_row_unknown_group_imports_uncategorised():
    client = FakeClient()
    row = normalize_export_row(export_row(txn_type="*SWI-SOMETHING", group="*SWI"))
    import_row(client, row, apply=True)
    assert "category_id" not in client.posts[0][1]


def test_import_row_dry_run_writes_nothing():
    client = FakeClient()
    result = import_row(client, normalize_export_row(export_row()), apply=False)
    assert result["dry_run"] is True
    assert client.posts == []


def test_category_map_covers_every_group_seen_in_real_export():
    for group in ("*CTR", "*TFI", "*CTRTAX", "*FEE", "*INS", "*INT", "*CLM"):
        assert group in CATEGORY_BY_GROUP


# --- reset_anchor ---

ACCOUNT = {
    "starting_balance": 135362.12,
    "starting_balance_date": "2026-09-01",
    "current_balance": 135362.12,
}


def test_anchor_skips_when_date_not_advanced():
    client = FakeClient(get_responses=[dict(ACCOUNT)])
    result = reset_anchor(client, 140000.0, "2026-09-01", apply=True)
    assert result["changed"] is False
    assert client.puts == []


def test_anchor_skips_sub_dollar_drift():
    client = FakeClient(get_responses=[dict(ACCOUNT)])
    result = reset_anchor(client, 135362.50, "2026-09-08", apply=True)
    assert result["changed"] is False
    assert client.puts == []


def test_anchor_resets_forward_on_real_drift():
    after = dict(ACCOUNT, starting_balance=135650.88, starting_balance_date="2026-09-08",
                 current_balance=135650.88)
    client = FakeClient(get_responses=[dict(ACCOUNT), after])
    result = reset_anchor(client, 135650.88, "2026-09-08", apply=True)
    assert client.puts == [
        ("/transaction_accounts/2141959", {"starting_balance": 135650.88, "starting_balance_date": "2026-09-08"})
    ]
    assert result["changed"] is True
    assert result["after"]["current_balance"] == 135650.88


def test_anchor_dry_run_reports_without_writing():
    client = FakeClient(get_responses=[dict(ACCOUNT)])
    result = reset_anchor(client, 135650.88, "2026-09-08", apply=False)
    assert result["changed"] == "dry-run"
    assert client.puts == []
