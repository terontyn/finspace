"""One deterministic categorization matcher shared by single and bulk evaluation.

Single matching used to run one active-rule query plus one category lookup per candidate rule, for
every transaction. Bulk preview would have turned that into an N+1 across thousands of rows, so the
rule set and every referenced target category are loaded once into a prepared, pure matcher that
both paths use. Semantics are unchanged: AND matchers, canonical ``priority, created_at, id`` order,
NFKC + whitespace + casefold substring comparison, explicit ``payee_id`` only, and the same
archived/incompatible target-category skipping.
"""

import re
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.categories import Category
from app.db.models.categorization_rules import CategorizationRule
from app.repositories import categorization_rules as rule_repository

_UNICODE_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)


def normalize_match_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _UNICODE_WHITESPACE.sub(" ", normalized.strip())
    return unicodedata.normalize("NFKC", normalized.casefold())


def category_compatible(transaction_type: str, category_type: str) -> bool:
    if transaction_type == "income":
        return category_type in {"income", "both"}
    if transaction_type == "expense":
        return category_type in {"expense", "both"}
    return transaction_type != "transfer"


class MatchableTransaction(Protocol):
    """The transaction attributes the matcher reads, so ORM rows and snapshots both fit."""

    transaction_type: str
    account_id: uuid.UUID
    payee_id: uuid.UUID | None
    counterparty: str | None
    description: str | None


@dataclass(frozen=True)
class MatchCandidate:
    """The transaction facts the matcher needs, normalized once per transaction."""

    transaction_type: str
    account_id: uuid.UUID
    payee_id: uuid.UUID | None
    counterparty: str
    description: str

    @classmethod
    def from_transaction(cls, transaction: MatchableTransaction) -> "MatchCandidate":
        return cls(
            transaction_type=transaction.transaction_type,
            account_id=transaction.account_id,
            payee_id=transaction.payee_id,
            counterparty=normalize_match_text(transaction.counterparty),
            description=normalize_match_text(transaction.description),
        )


@dataclass(frozen=True)
class PreparedRule:
    rule: CategorizationRule
    category: Category
    transaction_type: str | None
    account_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    counterparty_needle: str | None
    description_needle: str | None

    def matches(self, candidate: MatchCandidate) -> bool:
        if candidate.transaction_type == "transfer":
            return False
        if (
            self.transaction_type is not None
            and self.transaction_type != candidate.transaction_type
        ):
            return False
        if self.account_id is not None and self.account_id != candidate.account_id:
            return False
        if self.payee_id is not None and self.payee_id != candidate.payee_id:
            return False
        if (
            self.counterparty_needle is not None
            and self.counterparty_needle not in candidate.counterparty
        ):
            return False
        if (
            self.description_needle is not None
            and self.description_needle not in candidate.description
        ):
            return False
        return True


@dataclass(frozen=True)
class PreparedMatch:
    rule: CategorizationRule
    category: Category


@dataclass(frozen=True)
class PreparedRuleSet:
    """Active rules in canonical order, each already joined to a usable target category."""

    rules: tuple[PreparedRule, ...]

    def match(self, candidate: MatchCandidate) -> PreparedMatch | None:
        for prepared in self.rules:
            if not prepared.matches(candidate):
                continue
            if not category_compatible(candidate.transaction_type, prepared.category.category_type):
                # A rule whose target category cannot accept this transaction type is skipped, so a
                # later valid rule may still win.
                continue
            return PreparedMatch(rule=prepared.rule, category=prepared.category)
        return None

    def match_transaction(self, transaction: MatchableTransaction) -> PreparedMatch | None:
        return self.match(MatchCandidate.from_transaction(transaction))


async def prepare_rule_set(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    refresh: bool = False,
) -> PreparedRuleSet:
    """Load the deterministic rule set in two bounded queries regardless of candidate count."""
    rules = await rule_repository.active_rules(session, workspace_id, refresh=refresh)
    category_ids = {rule.category_id for rule in rules}
    categories: dict[uuid.UUID, Category] = {}
    if category_ids:
        statement = select(Category).where(
            Category.workspace_id == workspace_id,
            Category.id.in_(category_ids),
            Category.is_archived.is_(False),
        )
        if refresh:
            statement = statement.execution_options(populate_existing=True)
        categories = {
            category.id: category for category in (await session.scalars(statement)).all()
        }

    prepared: list[PreparedRule] = []
    for rule in rules:
        category = categories.get(rule.category_id)
        if category is None:
            # Missing or archived target category: the rule is skipped exactly as single matching
            # skips it, and evaluation continues with the next rule in canonical order.
            continue
        prepared.append(
            PreparedRule(
                rule=rule,
                category=category,
                transaction_type=rule.transaction_type,
                account_id=rule.account_id,
                payee_id=rule.payee_id,
                counterparty_needle=(
                    normalize_match_text(rule.counterparty_contains)
                    if rule.counterparty_contains is not None
                    else None
                ),
                description_needle=(
                    normalize_match_text(rule.description_contains)
                    if rule.description_contains is not None
                    else None
                ),
            )
        )
    return PreparedRuleSet(rules=tuple(prepared))
