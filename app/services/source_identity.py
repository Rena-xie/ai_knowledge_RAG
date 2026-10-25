"""来源 URL 身份、别名和正文去重。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utc_now_iso
from app.core.errors import AppError, ErrorCode
from app.domain.fingerprint import ContentFingerprint
from app.domain.source_links import RelatedDocumentLinks
from app.domain.url import normalize_url
from app.infra.db import KnowledgeSource, SourceAlias


@dataclass(frozen=True)
class IdentityResolution:
    """最终 URL 身份解析结果。"""

    source_id: int
    canonical_url: str
    duplicate_of_source_id: int | None = None
    duplicate_reason: str | None = None


@dataclass(frozen=True)
class IdentityPlanRequest:
    """最终 URL 身份规划输入。"""

    source_id: int
    final_url: str
    redirect_urls: tuple[str, ...] = ()
    related_links: RelatedDocumentLinks | None = None


@dataclass(frozen=True)
class IdentityConfirmation:
    """来源成功事务内要确认的身份。"""

    source_id: int
    submitted_url: str
    final_url: str
    redirect_urls: tuple[str, ...] = ()
    related_links: RelatedDocumentLinks | None = None


class SourceIdentityService:
    """规划最终 URL，并在成功事务内确认来源身份。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Callable[[], str] = utc_now_iso,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def plan_final_url(
        self,
        request: IdentityPlanRequest,
    ) -> IdentityResolution:
        """查询最终 URL 和声明链接是否已归属其他来源。"""

        canonical_url = normalize_url(request.final_url)
        candidates = self._alias_candidates(
            final_url=canonical_url,
            redirect_urls=request.redirect_urls,
            related_links=request.related_links,
        )
        with self._session_factory() as session:
            existing = self._find_conflicting_source(
                session, source_id=request.source_id, urls=candidates
            )
            if existing is not None:
                return IdentityResolution(
                    source_id=existing.id,
                    canonical_url=existing.canonical_url,
                    duplicate_of_source_id=existing.id,
                    duplicate_reason="duplicate_url",
                )
        return IdentityResolution(
            source_id=request.source_id,
            canonical_url=canonical_url,
        )

    def confirm_final_url(
        self,
        session: Session,
        confirmation: IdentityConfirmation,
    ) -> None:
        """在调用方事务内确认最终 URL 并写入来源别名。"""

        source = session.get(KnowledgeSource, confirmation.source_id)
        if source is None:
            raise AppError(
                ErrorCode.NOT_FOUND,
                f"来源不存在: {confirmation.source_id}",
            )
        canonical_url = normalize_url(confirmation.final_url)
        self._raise_if_conflict(
            session,
            confirmation=confirmation,
            canonical_url=canonical_url,
        )
        now = self._clock()
        previous_final_url = source.canonical_url
        source.canonical_url = canonical_url
        source.identity_status = "confirmed"
        source.identity_verified_at = now
        aliases = self._aliases_for_confirmation(
            confirmation,
            canonical_url=canonical_url,
            previous_final_url=previous_final_url,
        )
        self._upsert_aliases(
            session,
            confirmation.source_id,
            aliases,
            now=now,
        )

    def _raise_if_conflict(
        self,
        session: Session,
        *,
        confirmation: IdentityConfirmation,
        canonical_url: str,
    ) -> None:
        conflict = self._find_conflicting_source(
            session,
            source_id=confirmation.source_id,
            urls=self._alias_candidates(
                final_url=canonical_url,
                redirect_urls=confirmation.redirect_urls,
                related_links=confirmation.related_links,
            ),
        )
        if conflict is not None:
            raise AppError(
                ErrorCode.SOURCE_IDENTITY_CONFLICT,
                "最终 URL 已属于其他来源",
            )

    @staticmethod
    def _aliases_for_confirmation(
        confirmation: IdentityConfirmation,
        *,
        canonical_url: str,
        previous_final_url: str,
    ) -> list[tuple[str, str]]:
        aliases = [
            (confirmation.submitted_url, "submitted"),
            (canonical_url, "final"),
        ]
        if previous_final_url != canonical_url:
            aliases.append((previous_final_url, "previous_final"))
        aliases.extend((url, "redirect") for url in confirmation.redirect_urls)
        if confirmation.related_links is not None:
            related = confirmation.related_links
            if related.declared_canonical:
                aliases.append((related.declared_canonical, "declared_canonical"))
            if related.amp_html:
                aliases.append((related.amp_html, "amp"))
        return aliases

    def find_content_duplicate(
        self,
        fingerprint: ContentFingerprint,
        *,
        exclude_source_id: int,
    ) -> int | None:
        """返回拥有相同版本化正文指纹的最早已完成来源。"""

        with self._session_factory() as session:
            return session.scalar(
                select(KnowledgeSource.id)
                .where(
                    KnowledgeSource.content_hash == fingerprint.value,
                    KnowledgeSource.fingerprint_version == fingerprint.version,
                    KnowledgeSource.status == "completed",
                    KnowledgeSource.id != exclude_source_id,
                )
                .order_by(KnowledgeSource.id)
                .limit(1)
            )

    def matches_existing_content(
        self,
        source_id: int,
        fingerprint: ContentFingerprint,
    ) -> bool:
        """判断既有来源是否已使用相同版本的正文指纹。"""

        with self._session_factory() as session:
            source = session.get(KnowledgeSource, source_id)
            return bool(
                source is not None
                and source.status == "completed"
                and source.fingerprint_version == fingerprint.version
                and source.content_hash == fingerprint.value
            )

    def attach_submitted_alias(
        self,
        source_id: int,
        *,
        url: str,
    ) -> None:
        """把重复 URL 附加到既有来源。"""

        with self._session_factory() as session:
            self._upsert_aliases(
                session,
                source_id,
                [(url, "submitted")],
                now=self._clock(),
            )
            session.commit()

    def merge_aliases(self, *, from_source_id: int, to_source_id: int) -> None:
        """在删除临时重复来源前迁移其别名。"""

        with self._session_factory() as session:
            source_aliases = list(
                session.scalars(
                    select(SourceAlias).where(
                        SourceAlias.source_id == from_source_id
                    )
                )
            )
            target_urls = set(
                session.scalars(
                    select(SourceAlias.normalized_url).where(
                        SourceAlias.source_id == to_source_id
                    )
                )
            )
            for alias in source_aliases:
                if alias.normalized_url in target_urls:
                    session.execute(
                        delete(SourceAlias).where(SourceAlias.id == alias.id)
                    )
                else:
                    alias.source_id = to_source_id
                    target_urls.add(alias.normalized_url)
            session.commit()

    @staticmethod
    def _find_conflicting_source(
        session: Session,
        *,
        source_id: int,
        urls: Sequence[str],
    ) -> KnowledgeSource | None:
        normalized_urls = sorted(set(urls))
        if not normalized_urls:
            return None
        direct = list(
            session.scalars(
                select(KnowledgeSource).where(
                    KnowledgeSource.canonical_url.in_(normalized_urls),
                    KnowledgeSource.id != source_id,
                )
            )
        )
        aliased = list(
            session.scalars(
                select(KnowledgeSource)
                .join(SourceAlias, SourceAlias.source_id == KnowledgeSource.id)
                .where(
                    SourceAlias.normalized_url.in_(normalized_urls),
                    KnowledgeSource.id != source_id,
                )
            )
        )
        candidates = {source.id: source for source in (*direct, *aliased)}
        if not candidates:
            return None
        return min(
            candidates.values(),
            key=lambda source: (
                source.status != "completed",
                source.id,
            ),
        )

    @staticmethod
    def _alias_candidates(
        *,
        final_url: str,
        redirect_urls: Sequence[str],
        related_links: RelatedDocumentLinks | None,
    ) -> list[str]:
        candidates = [final_url]
        candidates.extend(normalize_url(url) for url in redirect_urls)
        if related_links is not None:
            candidates.extend(
                url
                for url in (
                    related_links.declared_canonical,
                    related_links.amp_html,
                )
                if url is not None
            )
        return candidates

    def _upsert_aliases(
        self,
        session: Session,
        source_id: int,
        aliases: Sequence[tuple[str, str]],
        *,
        now: str,
    ) -> None:
        for url, alias_type in aliases:
            if not url:
                continue
            normalized_url = normalize_url(url)
            existing = session.scalar(
                select(SourceAlias).where(
                    SourceAlias.normalized_url == normalized_url
                )
            )
            if existing is None:
                session.add(
                    SourceAlias(
                        source_id=source_id,
                        url=url,
                        normalized_url=normalized_url,
                        alias_type=alias_type,
                        created_at=now,
                    )
                )
            elif existing.source_id != source_id:
                raise AppError(
                    ErrorCode.SOURCE_IDENTITY_CONFLICT,
                    "URL 别名已属于其他来源",
                )
