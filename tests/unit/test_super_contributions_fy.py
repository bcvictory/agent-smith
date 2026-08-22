"""Tests for the REST fund-feed classifier and FY bucketing."""

from datetime import date

from scripts.operations.super_contributions_fy import classify, financial_year, fy_label, summarise


# One row of every shape the REST Industry Super account actually carries, plus an
# unknown row that must surface as a warning instead of being silently dropped.
SAMPLE_ROWS = [
    {
        "date": "2026-08-17",
        "payee": "THE TRUSTEE FOR DE GROOT ROOF PAINTING TRADING TRU",
        "category": "Super Contribution",
        "amount": 311.54,
    },
    {"date": "2026-06-19", "payee": "Voluntary contribution", "category": "Super Contribution", "amount": 14500.0},
    {"date": "2026-05-22", "payee": "Voluntary contribution", "category": "Super Contribution", "amount": 9000.0},
    {"date": "2026-06-20", "payee": "Contribution tax", "category": "Super Contribution", "amount": -3525.0},
    {"date": "2026-07-31", "payee": "Contribution tax", "category": "Super Contribution", "amount": -186.92},
    {"date": "2026-07-31", "payee": "Administration Fee -", "category": "Investments", "amount": -7.5},
    {"date": "2026-07-31", "payee": "Administration Fee (%)", "category": "Investments", "amount": -10.89},
    {
        "date": "2026-07-31",
        "payee": "Insurance premium: Standard Death cover",
        "category": None,
        "amount": -3.0,
    },
    {"date": "2026-06-30", "payee": "Investment earnings", "category": "Investments", "amount": 11128.48},
    {"date": "2026-06-29", "payee": "Mystery credit", "category": None, "amount": 42.0},
]


def test_financial_year_boundaries():
    assert financial_year(date(2026, 6, 30)) == 2026
    assert financial_year(date(2026, 7, 1)) == 2027
    assert fy_label(2026) == "FY2025-26"
    assert fy_label(2030) == "FY2029-30"


def test_classify_every_row_shape():
    buckets = [classify(row["payee"], row["category"] or "", row["amount"]) for row in SAMPLE_ROWS]
    assert buckets == [
        "employerSg",
        "personalDeductible",
        "personalDeductible",
        "contributionTax",
        "contributionTax",
        "feesAndInsurance",
        "feesAndInsurance",
        "feesAndInsurance",
        "earnings",
        "other",
    ]


def test_classify_falls_back_to_category_for_unnamed_employer_credit():
    assert classify("Contribution", "Super Contribution", 250.0) == "employerSg"
    assert classify("Contribution", "Super Contribution", -250.0) == "other"


def test_classify_historical_payee_wording():
    """REST renamed most payees around 2025; the older wording must bucket the same."""
    assert classify("Contributions Tax", "Super Contribution", -342.34) == "contributionTax"
    assert classify("Personal Conts Tax", "Tax", -2250.0) == "contributionTax"
    assert classify("Transfer In Tax", "Transfers", -0.49) == "contributionTax"
    assert classify("Admin Fee (%)", "Investments", -1.14) == "feesAndInsurance"
    assert classify("TAL Std Death Premium", "Insurance", -1.38) == "feesAndInsurance"
    assert classify("Investment Earnings", "Investments", 3493.07) == "earnings"
    # Bailey's own concessional top-ups, not employer SG, despite the category.
    assert classify("Member Cont", "Super Contribution", 15000.0) == "personalDeductible"
    assert classify("SG Voucher", "Investments", 3.26) == "employerSg"


def test_summarise_buckets_by_financial_year():
    result = summarise(SAMPLE_ROWS)
    by_fy = {row["fy"]: row for row in result["byFy"]}

    assert [row["fyLabel"] for row in result["byFy"]] == ["FY2025-26", "FY2026-27"]
    assert by_fy[2026]["personalDeductible"] == 23500.0
    assert by_fy[2026]["contributionTax"] == -3525.0
    assert by_fy[2026]["earnings"] == 11128.48
    assert by_fy[2026]["other"] == 42.0
    # net into fund = employer + personal + (negative) contribution tax
    assert by_fy[2026]["netIntoFund"] == 19975.0
    assert by_fy[2026]["lastReceivedDate"] == "2026-06-19"
    assert by_fy[2026]["lastReceivedAmount"] == 14500.0

    assert by_fy[2027]["employerSg"] == 311.54
    assert by_fy[2027]["feesAndInsurance"] == -21.39
    assert by_fy[2027]["netIntoFund"] == 124.62
    assert by_fy[2027]["lastReceivedDate"] == "2026-08-17"


def test_summarise_emits_contribution_history_rows_oldest_first():
    result = summarise(SAMPLE_ROWS)

    assert [(item["dateReceived"], item["type"]) for item in result["contributions"]] == [
        ("2026-05-22", "personal_deductible"),
        ("2026-06-19", "personal_deductible"),
        ("2026-08-17", "employer_sg"),
    ]
    assert all(item["fund"] == "REST" and item["source"] == "PocketSmith" for item in result["contributions"])


def test_summarise_warns_about_unclassified_rows():
    assert summarise(SAMPLE_ROWS)["warnings"] == ["unclassified 2026-06-29 Mystery credit +42.00"]
