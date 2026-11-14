from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlparse

from .types import RawEvent


def _contract_key(chain_id: str, address: str) -> tuple[str, str]:
    chain = chain_id.strip().lower()
    value = address.strip()
    if chain.startswith("evm") or chain in {"ethereum", "base", "arbitrum", "optimism", "polygon"}:
        value = value.lower()
    return chain, value


@dataclass(frozen=True, slots=True)
class EventEntity:
    project_id: str
    canonical_asset_id: str
    name: str
    tickers: tuple[str, ...]
    contracts: tuple[tuple[str, str], ...] = ()
    official_account_ids: tuple[str, ...] = ()
    official_domains: tuple[str, ...] = ()


class EntityResolver:
    """Contract/project/account-first resolution; ticker-only joins are forbidden."""

    def __init__(self, entities: tuple[EventEntity, ...] = ()):
        self._entities: dict[str, EventEntity] = {}
        self._contracts: dict[tuple[str, str], str] = {}
        self._accounts: dict[str, set[str]] = {}
        self._domains: dict[str, set[str]] = {}
        self._tickers: dict[str, set[str]] = {}
        for entity in entities:
            self.register(entity)

    def register(self, entity: EventEntity) -> None:
        if entity.project_id in self._entities:
            raise ValueError(f"duplicate project_id: {entity.project_id}")
        self._entities[entity.project_id] = entity
        for chain_id, address in entity.contracts:
            key = _contract_key(chain_id, address)
            existing = self._contracts.get(key)
            if existing and existing != entity.project_id:
                raise ValueError(f"contract collision: {key}")
            self._contracts[key] = entity.project_id
        for account in entity.official_account_ids:
            self._accounts.setdefault(account, set()).add(entity.project_id)
        for domain in entity.official_domains:
            self._domains.setdefault(domain.lower(), set()).add(entity.project_id)
        for ticker in entity.tickers:
            self._tickers.setdefault(ticker.upper(), set()).add(entity.project_id)

    def get(self, project_id: str) -> EventEntity | None:
        return self._entities.get(project_id)

    def resolve(self, raw: RawEvent) -> tuple[EventEntity, ...]:
        projects: set[str] = set()
        for chain_id, address in raw.contract_addresses:
            project = self._contracts.get(_contract_key(chain_id, address))
            if project:
                projects.add(project)
        if raw.explicit_project_id and raw.explicit_project_id in self._entities:
            projects.add(raw.explicit_project_id)
        if not projects and raw.author_id:
            matches = self._accounts.get(raw.author_id, set())
            if len(matches) == 1:
                projects.update(matches)
        if not projects:
            hostname = (urlparse(raw.url).hostname or "").lower()
            for domain, matches in self._domains.items():
                if hostname == domain or hostname.endswith("." + domain):
                    if len(matches) == 1:
                        projects.update(matches)
        return tuple(self._entities[key] for key in sorted(projects))

    def resolve_ticker(self, ticker: str) -> EventEntity | None:
        """Ticker is only a hint and is accepted only when globally unambiguous."""
        matches = self._tickers.get(ticker.upper(), set())
        if len(matches) != 1:
            return None
        return self._entities[next(iter(matches))]

    def known_contracts_in_text(self, text: str) -> tuple[tuple[str, str], ...]:
        """Only return exact registry contracts; arbitrary address-shaped text is not identity."""
        found: list[tuple[str, str]] = []
        lowered = text.lower()
        words = set(re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40}", text))
        for key in self._contracts:
            chain, address = key
            if address in words or address.lower() in lowered:
                found.append((chain, address))
        return tuple(sorted(set(found)))

    def all(self) -> tuple[EventEntity, ...]:
        return tuple(self._entities[key] for key in sorted(self._entities))
