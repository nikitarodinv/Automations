"""Cost ledger and budget guard. Every paid call is checked before and written right after."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import QuotaError
from .store import Store
from .util import iso, now_utc


@dataclass
class RunBudget:
    finder_credits: float
    llm_calls: int
    searches: int


class Ledger:
    def __init__(self, store: Store, run_id: str, budget: RunBudget):
        self.store = store
        self.run_id = run_id
        self.budget = budget

    def _next_seq(self) -> int:
        row = self.store.db.execute("SELECT MAX(seq) FROM ledger WHERE run_id=?", (self.run_id,)).fetchone()
        return (row[0] or 0) + 1

    def record(
        self,
        provider: str,
        endpoint: str,
        *,
        credits: float = 0,
        searches: int = 0,
        tokens: int = 0,
        llm_calls: int = 0,
        http_status: int | None = None,
    ) -> None:
        self.store.db.execute(
            "INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, self._next_seq(), provider, endpoint, credits, searches, tokens, llm_calls, http_status, iso()),
        )
        self.store.db.commit()

    def run_total(self, column: str, providers: list[str] | None = None) -> float:
        assert column in ("credits", "searches", "tokens", "llm_calls")
        q = f"SELECT COALESCE(SUM({column}),0) FROM ledger WHERE run_id=?"
        args: list = [self.run_id]
        if providers:
            q += f" AND provider IN ({','.join('?' * len(providers))})"
            args += providers
        return self.store.db.execute(q, args).fetchone()[0]

    def total_since(self, provider: str, since_iso: str, column: str = "credits") -> float:
        return self.store.db.execute(
            f"SELECT COALESCE(SUM({column}),0) FROM ledger WHERE provider=? AND at>=?", (provider, since_iso)
        ).fetchone()[0]

    def month_total(self, column: str, provider: str) -> float:
        start = now_utc().replace(day=1, hour=0, minute=0, second=0)
        return self.store.db.execute(
            f"SELECT COALESCE(SUM({column}),0) FROM ledger WHERE provider=? AND at>=?", (provider, iso(start))
        ).fetchone()[0]

    # ---- guards: call before spending ----
    def require_finder_credits(self, cost: float, finder_providers: list[str]) -> None:
        spent = self.run_total("credits", finder_providers)
        if spent + cost > self.budget.finder_credits + 1e-9:
            raise QuotaError(
                f"run credit budget reached ({spent:g} of {self.budget.finder_credits:g} used)"
            )

    def require_llm_call(self) -> None:
        if self.run_total("llm_calls") + 1 > self.budget.llm_calls:
            raise QuotaError(f"run model-call budget reached ({self.budget.llm_calls})")

    def require_search(self) -> None:
        if self.run_total("searches") + 1 > self.budget.searches:
            raise QuotaError(f"run search budget reached ({self.budget.searches})")
