"""Provider pool (section 4): every provider you have a free account with, treated as one pool.
Stage 1 draws from `providers.sources`, Stage 3 from `providers.finders`, each in config order.
The pool owns allowances, pacing, budgets and the ledger, so adapters never touch the pipeline."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from .errors import AuthError, ConfigError
from .ledger import Ledger
from .providers import EmailFinder, Enricher, MissingKey, PeopleSource, build, manifests
from .store import Store
from .util import iso

ENRICHERS = {"pdl": "pdl_enrich", "apollo": "apollo_enrich"}


def _next_reset(today: date, resets: str, reset_day: int | None) -> date:
    day = reset_day if resets == "signup_day" and reset_day else 1
    this = today.replace(day=min(day, calendar.monthrange(today.year, today.month)[1]))
    if this > today:
        return this
    y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def _period_start(today: date, resets: str, reset_day: int | None) -> date:
    nxt = _next_reset(today, resets, reset_day)
    y, m = (nxt.year - 1, 12) if nxt.month == 1 else (nxt.year, nxt.month - 1)
    return date(y, m, min(nxt.day, calendar.monthrange(y, m)[1]))


def _local_iso(d: date) -> str:
    return iso(datetime(d.year, d.month, d.day).astimezone())


@dataclass
class Allowance:
    """One provider's monthly allowance, shared by every list and campaign (pool rule 1)."""

    account: str
    monthly: float | None            # None = no credit limit (e.g. Apollo search)
    reserve: float = 5
    pace: str = "even"               # even | burst
    resets: str = "calendar_month"
    reset_day: int | None = None
    used_period: float = 0
    spent_today: float = 0
    balance: float | None = None     # what the provider itself reports (wins over the ledger)
    reset_date: str | None = None
    today: date = field(default_factory=date.today)

    @property
    def remaining(self) -> float:
        if self.balance is not None:
            return self.balance
        if self.monthly is None:
            return math.inf
        return max(0.0, self.monthly - self.used_period)

    @property
    def next_reset(self) -> date:
        if self.reset_date:
            try:
                return date.fromisoformat(str(self.reset_date)[:10])
            except ValueError:
                pass
        return _next_reset(self.today, self.resets, self.reset_day)

    @property
    def days_left(self) -> int:
        return max(1, (self.next_reset - self.today).days)

    def today_cap(self) -> float:
        """(allowance left at the start of today − reserve) ÷ days until it resets."""
        if math.isinf(self.remaining):
            return math.inf
        return max(0.0, (self.remaining + self.spent_today - self.reserve) / self.days_left)

    def can_spend(self, cost: float) -> bool:
        if cost <= 0:
            return True
        left = self.remaining - self.reserve
        if cost > left + 1e-9:
            return False
        if self.pace == "burst":
            return True
        # Even pacing, but always allow the first paid call of the day while credits remain.
        return self.spent_today + cost <= self.today_cap() + 1e-9 or self.spent_today == 0

    def available_today(self) -> float:
        if math.isinf(self.remaining):
            return math.inf
        left = max(0.0, self.remaining - self.reserve)
        if self.pace == "burst":
            return left
        return min(left, max(self.today_cap() - self.spent_today, 1.0 if self.spent_today == 0 and left >= 1 else 0.0))

    def charge(self, credits: float) -> None:
        self.spent_today += credits
        self.used_period += credits
        if self.balance is not None:
            self.balance = max(0.0, self.balance - credits)

    def summary(self) -> str:
        if self.monthly is None and self.balance is None:
            return f"{self.account}: no credit limit"
        total = self.monthly if self.monthly is not None else self.remaining + self.used_period
        return f"{self.account} {self.remaining:g}/{total:g} left · resets {self.next_reset.strftime('%b %-d')}"


class Pool:
    def __init__(self, cfg: Any, store: Store, ledger: Ledger, campaign: Any = None, finder_budget_check: Any = None):
        self.cfg = cfg
        self.store = store
        self.ledger = ledger
        self.campaign = campaign
        pc = cfg.data.get("providers") or {}
        self.pace = pc.get("pace", "even")
        self.reserve = float(pc.get("reserve", 5))
        self.source_entries: list[dict[str, Any]] = list(pc.get("sources") or [])
        self.finder_entries: list[dict[str, Any]] = list(pc.get("finders") or [])
        self.allowances: dict[str, Allowance] = {}
        self.skipped: dict[str, str] = {}          # name -> why (pool rule 2)
        self.exhausted: set[str] = set()           # accounts that hit a quota error this run
        self.finder_budget_check = finder_budget_check
        self._balances_checked: set[str] = set()

    # ------------------------------------------------------------------ allowances
    def _entry(self, account: str) -> dict[str, Any]:
        for e in self.source_entries + self.finder_entries:
            if e.get("name") == account and "monthly" in e:
                return e
        return {}

    def allowance(self, account: str) -> Allowance:
        if account in self.allowances:
            return self.allowances[account]
        m = manifests().get(account)
        plan = (m.free_plan if m else {}) or {}
        entry = self._entry(account)
        monthly = entry["monthly"] if "monthly" in entry else plan.get("monthly")
        budgets = self.cfg.data.get("budgets") or {}
        cap = (budgets.get("provider_monthly") or {}).get(account)
        if account == "hunter" and budgets.get("hunter_monthly") is not None:
            cap = budgets["hunter_monthly"] if cap is None else min(cap, budgets["hunter_monthly"])
        if cap is not None:
            monthly = cap if monthly is None else min(monthly, cap)
        a = Allowance(account=account, monthly=monthly, reserve=float(entry.get("reserve", self.reserve)), pace=self.pace,
                      resets=plan.get("resets", "calendar_month"), reset_day=entry.get("reset_day") or plan.get("reset_day"))
        start = _period_start(a.today, a.resets, a.reset_day)
        a.used_period = self.ledger.total_since(account, _local_iso(start))
        a.spent_today = self.ledger.total_since(account, _local_iso(a.today))
        self.allowances[account] = a
        return a

    def refresh_balance(self, inst: Any) -> None:
        """Hunter's free /v2/account: its number beats the ledger. A rejected key aborts the run here,
        before anything is spent."""
        acct = inst.account or inst.name
        if acct in self._balances_checked:
            return
        self._balances_checked.add(acct)
        bal = inst.balance()
        if bal is not None:
            a = self.allowance(acct)
            a.balance, a.reset_date = bal
            if a.monthly is not None:
                a.balance = min(a.balance, max(0.0, a.monthly - a.used_period))  # monthly budget still caps it

    def _wire(self, inst: Any, finder: bool) -> Any:
        acct = inst.account or inst.name

        def can_spend(credits: float) -> bool:
            if acct in self.exhausted:
                return False
            if finder and self.finder_budget_check and not self.finder_budget_check(credits):
                return False
            return self.allowance(acct).can_spend(credits) if inst.manifest else True

        def charge(endpoint: str, credits: float = 0, status: int | None = None) -> None:
            self.ledger.record(acct, endpoint, credits=credits, http_status=status)
            if inst.manifest:
                self.allowance(acct).charge(credits)

        inst.can_spend = can_spend
        inst.charge = charge
        if isinstance(inst, PeopleSource) and self.campaign is not None:
            cid = self.campaign.id
            inst.get_cursor = lambda key: self.store.get_cursor(cid, inst.name, key)
            inst.set_cursor = lambda key, value: self.store.set_cursor(cid, inst.name, key, value)
        return inst

    def _build(self, name: str, role: Any, finder: bool, options: dict[str, Any] | None = None) -> Any | None:
        try:
            inst = build(name, role, **(options or {}))
        except MissingKey as e:
            self.skipped[name] = str(e).split(": ", 1)[-1]
            return None
        inst = self._wire(inst, finder)
        if inst.manifest:
            try:
                self.refresh_balance(inst)
            except AuthError:
                raise
            except Exception as e:  # balance is a nicety; the ledger still counts
                self.skipped.setdefault(f"{name} balance", f"couldn't read balance ({type(e).__name__})")
        return inst

    # ------------------------------------------------------------------ roles
    def sources(self, source: str, options: dict[str, Any]) -> list[PeopleSource]:
        names = [e["name"] for e in self.source_entries] if source == "pool" else [source]
        if source == "pool" and not names:
            raise ConfigError("source: pool needs providers.sources in config.yaml")
        out = []
        for n in names:
            inst = self._build(n, PeopleSource, False, options if source != "pool" else {})
            if inst is None:
                continue
            if n == "urls":
                inst.enrichers = self.enrichers()
            out.append(inst)
        return out

    def finders(self) -> list[EmailFinder]:
        out = []
        for e in self.finder_entries:
            inst = self._build(e["name"], EmailFinder, True)
            if inst is not None:
                if e.get("cost"):
                    inst.cost = {**inst.cost, **e["cost"]}
                out.append(inst)
        return out

    def enrichers(self) -> list[Enricher]:
        out = []
        for e in self.source_entries + self.finder_entries:
            n = ENRICHERS.get(e["name"])
            if n and n not in [x.name for x in out]:
                inst = self._build(n, Enricher, False)
                if inst is not None:
                    out.append(inst)
        return out

    def summaries(self) -> list[str]:
        return [a.summary() for a in self.allowances.values()]
