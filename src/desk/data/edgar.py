"""EDGAR, read-only and free: the ticker-to-CIK map and each company's recent filings. The SEC asks
for a descriptive User-Agent with a contact; set DESK_SEC_USER_AGENT or the collector stays off."""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}/{document}"

# 8-K items, how much they tend to matter, and what they mean in words
ITEMS: dict[str, tuple[str, str]] = {
    "1.01": ("medium", "material agreement"),
    "1.02": ("medium", "termination of a material agreement"),
    "1.03": ("high", "bankruptcy or receivership"),
    "1.05": ("high", "cybersecurity incident"),
    "2.01": ("high", "acquisition or disposition of assets"),
    "2.02": ("high", "results of operations"),
    "2.03": ("medium", "new debt or obligation"),
    "2.04": ("high", "triggering event on a debt obligation"),
    "2.05": ("medium", "exit or disposal costs"),
    "2.06": ("medium", "material impairment"),
    "3.01": ("high", "delisting notice"),
    "3.02": ("low", "unregistered sale of equity"),
    "3.03": ("low", "change to security holder rights"),
    "4.01": ("high", "change of auditor"),
    "4.02": ("high", "non-reliance on past financial statements"),
    "5.01": ("high", "change in control"),
    "5.02": ("medium", "officer or director change"),
    "5.03": ("low", "bylaw or charter change"),
    "5.07": ("low", "shareholder vote results"),
    "5.08": ("low", "shareholder nominations"),
    "7.01": ("low", "Regulation FD disclosure"),
    "8.01": ("low", "other events"),
}
FORM_IMPACT: dict[str, tuple[str, str]] = {
    "10-Q": ("medium", "quarterly report"),
    "10-K": ("medium", "annual report"),
    "10-K/A": ("low", "amended annual report"),
    "10-Q/A": ("low", "amended quarterly report"),
    "SC 13D": ("medium", "activist stake above 5%"),
    "SC 13D/A": ("low", "amended activist stake"),
    "DEF 14A": ("low", "proxy statement"),
}
WATCHED_FORMS = {"8-K", "8-K/A", *FORM_IMPACT}
RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Filing:
    symbol: str
    cik: str
    accession: str
    form: str
    filed_at: datetime
    items: list[str]
    document: str
    description: str
    url: str


def impact_for(form: str, items: list[str]) -> tuple[str, str]:
    """(impact, what it is about) for a filing, from its form and its 8-K items."""
    if form.startswith("8-K"):
        best: tuple[str, str] | None = None
        labels = []
        for code in items:
            hit = ITEMS.get(code)
            if hit is None:
                continue
            labels.append(hit[1])
            if best is None or RANK[hit[0]] > RANK[best[0]]:
                best = hit
        if best is None:
            return "low", "8-K with no itemised content"
        return best[0], "; ".join(labels)
    return FORM_IMPACT.get(form, ("low", form))


def ticker_to_cik(data: dict[str, Any]) -> dict[str, str]:
    """The SEC's company_tickers.json: {"0": {"cik_str": 320193, "ticker": "AAPL", ...}, ...}.
    Tickers come as BRK-B; the desk writes BRK.B."""
    out: dict[str, str] = {}
    for row in data.values():
        ticker = str(row.get("ticker", "")).upper().replace("-", ".")
        if ticker:
            out[ticker] = f"{int(row['cik_str']):010d}"
    return out


def parse_submissions(data: dict[str, Any], symbol: str, cik: str) -> list[Filing]:
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    out: list[Filing] = []
    for i, form in enumerate(forms):
        if form not in WATCHED_FORMS:
            continue
        accession = str(recent["accessionNumber"][i])
        accepted = recent.get("acceptanceDateTime", [None] * len(forms))[i]
        filed = recent.get("filingDate", [None] * len(forms))[i]
        if accepted:
            filed_at = datetime.fromisoformat(str(accepted).replace("Z", "+00:00"))
            if filed_at.tzinfo is None:
                filed_at = filed_at.replace(tzinfo=UTC)
        else:
            filed_at = datetime.fromisoformat(str(filed)).replace(tzinfo=UTC)
        items_raw = recent.get("items", [""] * len(forms))[i] or ""
        items = [x.strip() for x in str(items_raw).split(",") if x.strip()]
        document = str(recent.get("primaryDocument", [""] * len(forms))[i] or "")
        description = str(recent.get("primaryDocDescription", [""] * len(forms))[i] or "")
        out.append(
            Filing(
                symbol=symbol,
                cik=cik,
                accession=accession,
                form=form,
                filed_at=filed_at.astimezone(UTC),
                items=items,
                document=document,
                description=description,
                url=ARCHIVE_URL.format(
                    cik_int=int(cik), accession=accession.replace("-", ""), document=document
                ),
            )
        )
    return out


class Edgar:
    def __init__(self, user_agent: str, *, timeout: float = 20.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def _get(self, url: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url, headers={"User-Agent": self.user_agent, "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)

    async def tickers(self) -> dict[str, str]:
        return ticker_to_cik(await asyncio.to_thread(self._get, TICKERS_URL))

    async def submissions(self, symbol: str, cik: str) -> list[Filing]:
        data = await asyncio.to_thread(self._get, SUBMISSIONS_URL.format(cik=cik))
        return parse_submissions(data, symbol, cik)
