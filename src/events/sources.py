from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
import time
from typing import Any
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import aiohttp
import structlog
from src.util.redaction import safe_exception

from .entities import EntityResolver
from .types import AuthenticityState, RawEvent, RelationType, SourceTier, payload_hash


Emit = Callable[[RawEvent], Awaitable[None]]


def _url_allowed(url: str, settings: dict[str, Any]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    allowed = tuple(str(item).lower() for item in settings.get("allowed_domains", []))
    return bool(host and allowed and any(host == domain or host.endswith("." + domain) for domain in allowed))


def validate_source_settings(source_id: str, settings: dict[str, Any]) -> None:
    for key in settings:
        lowered = str(key).lower()
        if any(token in lowered for token in ("api_key", "secret", "private_key", "bearer_token")) and not lowered.endswith("_env"):
            raise ValueError(f"{source_id}: credentials must be referenced through an *_env setting")
    configured_urls = [
        value for key in ("url", "api_url", "post_url_template", "web_url")
        if isinstance((value := settings.get(key)), str) and value
    ]
    if not configured_urls or any(urlparse(url).scheme != "https" for url in configured_urls):
        raise ValueError(f"{source_id}: HTTPS API/feed URLs are required")
    if settings.get("official", False) and any(not _url_allowed(url, settings) for url in configured_urls):
        raise ValueError(f"{source_id}: official source URL is outside allowed_domains")
    timeout = float(settings.get("request_timeout_seconds", settings.get("connect_timeout_seconds", 8)))
    if not 0 < timeout <= 10:
        raise ValueError(f"{source_id}: HTTP/connect timeout must be in (0,10]")


def _utc_ms(value: str | None, fallback_ms: int) -> int:
    if not value:
        return fallback_ms
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return fallback_ms
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


@dataclass(slots=True)
class SourceRuntime:
    status: str = "disabled"
    last_received_at_ms: int | None = None
    last_successful_poll_at_ms: int | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


class EventSource:
    def __init__(self, source_id: str, settings: dict[str, Any], resolver: EntityResolver):
        self.source_id = source_id
        self.settings = settings
        self.resolver = resolver
        self.source_tier = SourceTier(str(settings["source_tier"]))
        enabled = bool(settings.get("enabled", False))
        self.runtime = SourceRuntime(
            "starting" if enabled else "disabled",
            last_error=(None if enabled else str(settings.get("disabled_reason", "disabled by configuration"))[:500]),
        )
        self.log = structlog.get_logger("event_source").bind(source=source_id)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", False))

    @staticmethod
    def retry_delay(base_interval: float, failure_count: int, max_backoff: float) -> float:
        base = max(15.0, float(base_interval))
        cap = max(base, min(86_400.0, float(max_backoff)))
        return min(cap, base * (2 ** min(max(0, int(failure_count) - 1), 8)))

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        base_interval = max(15.0, float(self.settings.get("poll_interval_seconds", 300)))
        max_backoff = max(
            base_interval,
            min(86_400.0, float(self.settings.get("max_retry_backoff_seconds", 86_400))),
        )
        while not stop.is_set():
            delay = base_interval
            try:
                events = await self.poll()
                self.runtime.last_successful_poll_at_ms = int(time.time() * 1000)
                for event in events:
                    await emit(event)
                    self.runtime.last_received_at_ms = max(
                        self.runtime.last_received_at_ms or 0, event.received_at_ms
                    )
                self.runtime.status = "healthy"
                self.runtime.consecutive_failures = 0
                self.runtime.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.status = "degraded"
                self.runtime.consecutive_failures += 1
                self.runtime.last_error = safe_exception(exc)
                self.log.warning("event_source_poll_failed", error=safe_exception(exc))
                delay = self.retry_delay(
                    base_interval, self.runtime.consecutive_failures, max_backoff
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def poll(self) -> list[RawEvent]:
        raise NotImplementedError


class RSSAtomSource(EventSource):
    async def poll(self) -> list[RawEvent]:
        timeout = min(10.0, max(0.5, float(self.settings.get("request_timeout_seconds", 8))))
        headers = {"User-Agent": "hyperliquid-analyzer-event-intelligence/1.0"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as session:
            async with session.get(str(self.settings["url"])) as response:
                response.raise_for_status()
                body = await response.text()
        received = int(time.time() * 1000)
        return self.parse(body, received)

    def parse(self, body: str, received_at_ms: int) -> list[RawEvent]:
        root = ET.fromstring(body)
        result: list[RawEvent] = []
        entries = root.findall(".//item")
        if not entries:
            entries = [item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "entry"]
        limit = max(1, min(100, int(self.settings.get("max_items_per_poll", 30))))
        for entry in entries[:limit]:
            children = {item.tag.rsplit("}", 1)[-1]: item for item in list(entry)}
            def first(*names: str) -> ET.Element | None:
                return next((children[name] for name in names if name in children), None)
            title = (children.get("title").text if children.get("title") is not None else "") or ""
            description_node = first("description", "summary", "content")
            description = (description_node.text if description_node is not None else "") or ""
            link_node = children.get("link")
            link = ""
            if link_node is not None:
                link = link_node.attrib.get("href") or link_node.text or ""
            link = urljoin(str(self.settings["url"]), link.strip())
            guid_node = first("guid", "id")
            guid = ((guid_node.text if guid_node is not None else None) or link or title).strip()
            date_node = first("pubDate", "published", "updated")
            published = _utc_ms(date_node.text if date_node is not None else None, received_at_ms)
            if published > received_at_ms:
                published = received_at_ms
            text = " ".join((title.strip(), description.strip())).strip()
            payload = {"title": title, "description": description, "guid": guid, "link": link}
            project_id = self.settings.get("project_id")
            contracts = self.resolver.known_contracts_in_text(text)
            raw_id = f"{self.source_id}:{hashlib.sha256(guid.encode('utf-8')).hexdigest()[:24]}"
            result.append(RawEvent.build(
                raw_event_id=raw_id,
                source_id=self.source_id,
                source_tier=self.source_tier,
                author_id=str(self.settings.get("author_id")) if self.settings.get("author_id") else None,
                url=link or str(self.settings["url"]),
                published_at_ms=published,
                received_at_ms=received_at_ms,
                payload=payload,
                text=text,
                language=self.settings.get("language"),
                authenticity=(
                    AuthenticityState.VERIFIED
                    if self.settings.get("official", False) and _url_allowed(link or str(self.settings["url"]), self.settings)
                    else AuthenticityState.UNVERIFIED
                ),
                explicit_project_id=str(project_id) if project_id else None,
                contract_addresses=contracts,
            ))
        return result


class GitHubSource(EventSource):
    async def poll(self) -> list[RawEvent]:
        owner, repo = str(self.settings["repository"]).split("/", 1)
        resource = str(self.settings.get("resource", "releases"))
        if resource not in {"releases", "security-advisories"}:
            raise ValueError("GitHub resource must be releases or security-advisories")
        base = str(self.settings.get("api_url", "https://api.github.com")).rstrip("/")
        url = f"{base}/repos/{owner}/{repo}/{resource}"
        timeout = min(10.0, max(0.5, float(self.settings.get("request_timeout_seconds", 8))))
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hyperliquid-analyzer-event-intelligence/1.0",
        }
        token_env = str(self.settings.get("token_env", ""))
        token = os.getenv(token_env) if token_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as session:
            async with session.get(url, params={"per_page": 30}) as response:
                response.raise_for_status()
                values = await response.json()
        received = int(time.time() * 1000)
        return self.parse(values, received)

    def parse(self, values: list[dict[str, Any]], received: int) -> list[RawEvent]:
        owner, repo = str(self.settings["repository"]).split("/", 1)
        events = []
        for item in values:
            published = _utc_ms(item.get("published_at") or item.get("created_at"), received)
            text = " ".join((str(item.get("name") or item.get("summary") or ""), str(item.get("body") or item.get("description") or ""))).strip()
            html_url = str(item.get("html_url") or str(self.settings.get("web_url", "")).format(owner=owner, repo=repo))
            if not html_url:
                raise ValueError("GitHub response has no html_url and web_url is not configured")
            payload = {
                "id": item.get("id") or item.get("ghsa_id"),
                "tag_name": item.get("tag_name"),
                "name": item.get("name"),
                "body": item.get("body") or item.get("description"),
                "html_url": html_url,
            }
            stable_id = item.get("id") or item.get("ghsa_id") or payload_hash(payload)[:24]
            events.append(RawEvent.build(
                raw_event_id=f"{self.source_id}:{stable_id}", source_id=self.source_id,
                source_tier=self.source_tier,
                author_id=str((item.get("author") or {}).get("id") or owner), url=html_url,
                published_at_ms=min(published, received), received_at_ms=received, payload=payload,
                text=text, language=self.settings.get("language", "en"),
                authenticity=(
                    AuthenticityState.VERIFIED
                    if self.settings.get("official", False)
                    and _url_allowed(str(self.settings.get("api_url", "")), self.settings)
                    else AuthenticityState.UNVERIFIED
                ),
                explicit_project_id=self.settings.get("project_id"),
                contract_addresses=self.resolver.known_contracts_in_text(text),
            ))
        return events


class ICSCalendarSource(EventSource):
    async def poll(self) -> list[RawEvent]:
        timeout = min(10.0, max(0.5, float(self.settings.get("request_timeout_seconds", 8))))
        headers = {"User-Agent": "hyperliquid-analyzer-event-intelligence/1.0"}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), headers=headers) as session:
            async with session.get(str(self.settings["url"])) as response:
                response.raise_for_status()
                body = await response.text()
        return self.parse(body, int(time.time() * 1000))

    def parse(self, body: str, received_at_ms: int) -> list[RawEvent]:
        unfolded = body.replace("\r\n ", "").replace("\n ", "")
        blocks = unfolded.split("BEGIN:VEVENT")[1:]
        result = []
        for block in blocks[: max(1, min(500, int(self.settings.get("max_items_per_poll", 200))))]:
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                fields[key.split(";", 1)[0]] = value.strip()
            uid = fields.get("UID") or payload_hash(fields)
            summary = fields.get("SUMMARY", "Scheduled macro release")
            created = fields.get("DTSTAMP") or fields.get("CREATED")
            published = received_at_ms
            if created:
                try:
                    published = int(datetime.strptime(created[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                except ValueError:
                    published = received_at_ms
            scheduled = fields.get("DTSTART")
            payload = {
                "event_type": "MACRO",
                "uid": uid,
                "summary": summary,
                "scheduled_at": scheduled,
                "description": fields.get("DESCRIPTION"),
            }
            result.append(RawEvent.build(
                raw_event_id=f"{self.source_id}:{hashlib.sha256(uid.encode('utf-8')).hexdigest()[:24]}",
                source_id=self.source_id, source_tier=self.source_tier,
                author_id=str(self.settings.get("author_id") or self.source_id),
                url=str(self.settings["url"]), published_at_ms=min(published, received_at_ms),
                received_at_ms=received_at_ms, payload=payload, text=summary,
                language=self.settings.get("language", "en"),
                authenticity=(
                    AuthenticityState.VERIFIED
                    if self.settings.get("official", False)
                    and _url_allowed(str(self.settings.get("url", "")), self.settings)
                    else AuthenticityState.UNVERIFIED
                ),
            ))
        return result


class XFilteredStreamSource(EventSource):
    async def poll(self) -> list[RawEvent]:
        raise RuntimeError("X Filtered Stream is a streaming source")

    async def run(self, emit: Emit, stop: asyncio.Event) -> None:
        token_env = str(self.settings.get("bearer_token_env", "X_BEARER_TOKEN"))
        token = os.getenv(token_env)
        if not token:
            self.runtime.status = "disabled_missing_credentials"
            self.runtime.last_error = f"missing environment variable {token_env}"
            return
        url = str(self.settings["url"])
        connect_timeout = min(10.0, max(0.5, float(self.settings.get("connect_timeout_seconds", 8))))
        read_timeout = min(60.0, max(5.0, float(self.settings.get("read_timeout_seconds", 45))))
        while not stop.is_set():
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=connect_timeout, sock_read=read_timeout)
                headers = {"Authorization": f"Bearer {token}", "User-Agent": "hyperliquid-analyzer-event-intelligence/1.0"}
                params = {
                    "tweet.fields": "id,text,author_id,conversation_id,created_at,lang,referenced_tweets",
                    "expansions": "author_id,referenced_tweets.id",
                }
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url, params=params) as response:
                        response.raise_for_status()
                        self.runtime.status = "healthy"
                        self.runtime.last_successful_poll_at_ms = int(time.time() * 1000)
                        async for line in response.content:
                            self.runtime.last_successful_poll_at_ms = int(time.time() * 1000)
                            if stop.is_set():
                                return
                            if not line.strip():
                                continue
                            raw = self.parse_record(json.loads(line), int(time.time() * 1000))
                            await emit(raw)
                            self.runtime.last_received_at_ms = raw.received_at_ms
                            self.runtime.consecutive_failures = 0
                            self.runtime.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.runtime.status = "degraded"
                self.runtime.consecutive_failures += 1
                self.runtime.last_error = safe_exception(exc)
                self.log.warning("x_stream_disconnected", error=safe_exception(exc))
                try:
                    retry_cap = min(60.0, max(1.0, float(self.settings.get("reconnect_max_seconds", 30))))
                    await asyncio.wait_for(stop.wait(), timeout=min(retry_cap, 2 ** min(5, self.runtime.consecutive_failures)))
                except TimeoutError:
                    pass

    def parse_record(self, value: dict[str, Any], received_at_ms: int) -> RawEvent:
        data = value.get("data") or {}
        references = data.get("referenced_tweets") or []
        relation = RelationType.ORIGINAL
        related = None
        for item in references:
            mapped = {"retweeted": RelationType.REPOST, "quoted": RelationType.QUOTE, "replied_to": RelationType.REPLY}.get(item.get("type"))
            if mapped:
                relation, related = mapped, str(item.get("id"))
                break
        text = str(data.get("text") or "")
        post_id = str(data["id"])
        author_id = str(data.get("author_id") or "") or None
        published = _utc_ms(data.get("created_at"), received_at_ms)
        return RawEvent.build(
            raw_event_id=f"{self.source_id}:{post_id}", source_id=self.source_id,
            source_tier=self.source_tier, author_id=author_id,
            url=str(self.settings["post_url_template"]).format(post_id=post_id),
            published_at_ms=min(published, received_at_ms),
            received_at_ms=received_at_ms, payload=value, text=text, language=data.get("lang"),
            relation_type=relation, related_event_id=(f"{self.source_id}:{related}" if related else None),
            thread_id=str(data.get("conversation_id") or related or post_id),
            authenticity=(
                AuthenticityState.VERIFIED
                if self.settings.get("official", False)
                and author_id in set(self.settings.get("verified_account_ids", []))
                else AuthenticityState.UNVERIFIED
            ),
            explicit_project_id=(self.settings.get("account_project_ids") or {}).get(author_id),
            contract_addresses=self.resolver.known_contracts_in_text(text),
        )


def build_sources(settings: dict[str, dict[str, Any]], resolver: EntityResolver) -> dict[str, EventSource]:
    adapters = {
        "rss_atom": RSSAtomSource,
        "github": GitHubSource,
        "ics_calendar": ICSCalendarSource,
        "x_filtered_stream": XFilteredStreamSource,
    }
    result: dict[str, EventSource] = {}
    for source_id, config in settings.items():
        validate_source_settings(source_id, config)
        adapter = str(config.get("adapter", ""))
        if adapter not in adapters:
            raise ValueError(f"unsupported event source adapter {adapter!r} for {source_id}")
        result[source_id] = adapters[adapter](source_id, config, resolver)
    return result
