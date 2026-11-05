"""构建只包含文件系统事实、不做语言或关键词判断的本地证据。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Any, Callable, Sequence
import urllib.error

from .contracts import ANALYSIS_SCHEMA_VERSION, canonical_sha256
from .text_normalization import (
    sanitize_search_query,
    search_query_is_from_basename,
    title_identity_key,
)


def _candidate_language_title(
    candidate: dict[str, Any], field: str,
) -> Any:
    """有 Release 回源档案时，只从唯一 Release 读取语言字段。"""
    release_pages = candidate.get("release_pages") or []
    if release_pages:
        if len(release_pages) != 1 or not isinstance(release_pages[0], dict):
            return None
        return release_pages[0].get(field)
    return candidate.get(field)


def _covered_language_title(
    candidates: list[dict[str, Any]], field: str | None, keyword: Any,
) -> bool:
    """已有候选仅在返回该语言原名的精确标题时覆盖后续查询。"""
    if not field:
        return False
    values = [
        _candidate_language_title(item, field)
        for item in candidates
        if _candidate_language_title(item, field)
    ]
    query = sanitize_search_query(str(keyword or ""))
    if not query:
        return bool(values)
    expected = title_identity_key(query)
    return any(title_identity_key(str(value)) == expected for value in values)


def _network_error_text(exc: Exception) -> str:
    """保留可操作的 HTTP 状态码，其他异常保持稳定类型名。"""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return type(exc).__name__


def _retryable_network_error(error: Any) -> bool:
    """第二轮只处理超时、HTTP 和连接错误。"""
    value = str(error or "")
    return bool(re.fullmatch(
        r"HTTP \d{3}|(?:TimeoutError|timeout|ConnectionError|URLError|"
        r"ConnectionResetError|ConnectionRefusedError|"
        r"ConnectionAbortedError|RemoteDisconnected|gaierror)",
        value,
    ))


def _cache_provenance(source: Any) -> dict[str, Any]:
    read = getattr(source, "cache_provenance", None)
    return {"cache": read()} if read is not None else {}


def _search_one_source(
    local: dict[str, Any],
    semantics: dict[str, Any],
    source: Any,
    serialize_candidate: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """顺序执行单一来源的语言轮次；来源之间由调用方并发。"""
    source_id_keys = {"VNDB": "vndb", "DLsite": "dlsite", "Steam": "steam"}
    keywords = semantics["titles"]["search_keywords"]
    rounds: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    queried: set[str] = set()
    identifiers = local.get("source_ids") or {}
    source_key = source_id_keys.get(str(source.name), str(source.name).casefold())
    direct_pending = bool(identifiers.get(source_key))
    has_title_query = any(
        sanitize_search_query(str(keywords.get(language) or ""))
        for language in ("jp", "zh", "en")
    )
    for number, language, field in (
        (1, "jp", "jp_title"),
        (2, "zh", "zh_title"),
        (3, "en", "en_title"),
    ):
        covered = _covered_language_title(collected, field, keywords.get(language))
        if covered:
            rounds.append({
                "number": number, "language": language,
                "query": keywords.get(language),
                "status": "covered", "candidates": [],
            })
            continue
        query = sanitize_search_query(str(keywords.get(language) or ""))
        if not query:
            if direct_pending and not has_title_query:
                direct_pending = False
                try:
                    found = source.search(
                        [], identifiers,
                        (semantics.get("developer_evidence") or {}).get("value"),
                    )
                    serialized = [serialize_candidate(item) for item in found]
                    error = None
                except Exception as exc:
                    serialized = []
                    error = _network_error_text(exc)
                for item in serialized:
                    identity = (
                        str(item.get("source") or source.name),
                        str(item.get("source_id") or item.get("url") or ""),
                    )
                    if identity not in seen:
                        seen.add(identity)
                        collected.append(item)
                rounds.append({
                    "number": number, "language": language, "query": None,
                    "status": "success" if serialized else "failed",
                    "candidates": serialized,
                    "used_source_identifiers": True,
                    "discovery_method": "source_identifier",
                    **({"error": error} if error else {}),
                    **_cache_provenance(source),
                })
                continue
            rounds.append({
                "number": number, "language": language, "query": None,
                "status": "missing", "candidates": [],
            })
            continue
        if (
            not search_query_is_from_basename(
                query, str(local.get("raw_name") or "")
            )
            and (semantics.get("authorized_search_query_exceptions") or {}).get(
                language
            ) != query
        ):
            raise ValueError(
                f"{language} 搜索关键词必须来自原文件夹名称："
                f"{local.get('game_id')}"
            )
        query_key = query.casefold()
        if query_key in queried:
            rounds.append({
                "number": number, "language": language, "query": query,
                "status": "covered", "candidates": [],
                "covered_reason": "duplicate_query",
            })
            continue
        queried.add(query_key)
        query_ids = identifiers if direct_pending else {
            "vndb": [], "dlsite": [], "steam": [],
        }
        direct_pending = False
        try:
            found = source.search(
                [query],
                query_ids,
                (semantics.get("developer_evidence") or {}).get("value"),
            )
            serialized = [serialize_candidate(item) for item in found]
            status = "success" if serialized else "failed"
            error = None
        except Exception as exc:
            serialized = []
            status = "failed"
            error = _network_error_text(exc)
        for item in serialized:
            identity = (
                str(item.get("source") or source.name),
                str(item.get("source_id") or item.get("url") or ""),
            )
            if identity not in seen:
                seen.add(identity)
                collected.append(item)
        round_payload = {
            "number": number, "language": language, "query": query,
            "status": status, "candidates": serialized,
            **({"error": error} if error else {}),
            **_cache_provenance(source),
        }
        if any(query_ids.get(key) for key in source_id_keys.values()):
            round_payload["used_source_identifiers"] = True
        rounds.append(round_payload)
    return {"source": source.name, "rounds": rounds, "candidates": collected}


def _search_source_batch(
    accepted: Sequence[tuple[str, dict[str, Any]]],
    game_evidence: dict[str, dict[str, Any]],
    source: Any,
    source_index: int,
    serialize_candidate: Callable[[Any], dict[str, Any]],
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[tuple[str, int], dict[str, Any]]:
    """同一来源串行查询；三个来源之间并发，避免共享客户端被并发重入。"""
    reports: dict[tuple[str, int], dict[str, Any]] = {}
    total = len(accepted)
    for completed, (game_id, semantics) in enumerate(accepted, start=1):
        begin_budget = getattr(source, "begin_search_budget", None)
        end_budget = getattr(source, "end_search_budget", None)
        if begin_budget is not None:
            begin_budget()
        try:
            reports[(game_id, source_index)] = _search_one_source(
                game_evidence[game_id], semantics, source, serialize_candidate
            )
        finally:
            if end_budget is not None:
                end_budget()
        if progress is not None:
            progress(str(source.name), completed, total)
    return reports


def _search_game_source(
    game_id: str,
    semantics: dict[str, Any],
    local: dict[str, Any],
    source: Any,
    serialize_candidate: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """在独立线程预算内搜索一个游戏的一个来源。"""
    begin_budget = getattr(source, "begin_search_budget", None)
    end_budget = getattr(source, "end_search_budget", None)
    if begin_budget is not None:
        begin_budget()
    try:
        return _search_one_source(local, semantics, source, serialize_candidate)
    finally:
        if end_budget is not None:
            end_budget()


def _retry_network_error_rounds(
    local: dict[str, Any],
    semantics: dict[str, Any],
    source: Any,
    serialize_candidate: Callable[[Any], dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    """首轮全部结束后，仅统一重试带网络错误的语言轮一次。"""
    source_id_keys = {"VNDB": "vndb", "DLsite": "dlsite", "Steam": "steam"}
    source_key = source_id_keys.get(str(source.name), str(source.name).casefold())
    identifiers = local.get("source_ids") or {}
    empty_identifiers = {"vndb": [], "dlsite": [], "steam": []}
    developer = (semantics.get("developer_evidence") or {}).get("value")
    field_by_language = {"jp": "jp_title", "zh": "zh_title", "en": "en_title"}
    rounds = [dict(item) for item in report.get("rounds", [])]
    collected = list(report.get("candidates", []))
    seen = {
        (
            str(item.get("source") or source.name),
            str(item.get("source_id") or item.get("url") or ""),
        )
        for item in collected
    }
    for round_item in rounds:
        if not _retryable_network_error(round_item.get("error")):
            continue
        prior_error = str(round_item["error"])
        retry_count = int(round_item.get("retry_count") or 0) + 1
        language = str(round_item.get("language") or "")
        field = field_by_language.get(language)
        covered = _covered_language_title(
            collected, field, round_item.get("query"),
        )
        if covered:
            round_item.update({
                "status": "covered", "candidates": [],
                "prior_error": prior_error,
            })
            round_item.pop("error", None)
            continue
        query = sanitize_search_query(str(round_item.get("query") or ""))
        query_ids = (
            identifiers
            if round_item.get("used_source_identifiers")
            and identifiers.get(source_key)
            else empty_identifiers
        )
        try:
            found = source.search([query], query_ids, developer)
            serialized = [serialize_candidate(item) for item in found]
            round_item.update({
                "status": "success" if serialized else "failed",
                "candidates": serialized, "retry_count": retry_count,
                "prior_error": prior_error,
                **_cache_provenance(source),
            })
            round_item.pop("error", None)
        except Exception as exc:
            serialized = []
            round_item.update({
                "status": "failed", "candidates": [],
                "error": _network_error_text(exc),
                "retry_count": retry_count,
                "prior_error": prior_error,
                **_cache_provenance(source),
            })
        for item in serialized:
            identity = (
                str(item.get("source") or source.name),
                str(item.get("source_id") or item.get("url") or ""),
            )
            if identity not in seen:
                seen.add(identity)
                collected.append(item)
    return {"source": source.name, "rounds": rounds, "candidates": collected}


def retry_search_network_errors(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    previous_evidence: dict[str, Any],
    sources: Sequence[Any],
    serialize_candidate: Callable[[Any], dict[str, Any]],
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """从已审计证据续跑，仅再次请求仍带网络错误的语言轮。"""
    semantics_by_id = {
        item["game_id"]: item for item in local_semantics.get("games", [])
    }
    expected_analysis = analysis.get("input_fingerprint")
    expected_local = canonical_sha256(local_semantics)
    if previous_evidence.get("analysis_fingerprint") != expected_analysis:
        raise ValueError("旧搜索证据与分析输入摘要不一致")
    if previous_evidence.get("local_semantics_fingerprint") != expected_local:
        raise ValueError("旧搜索证据与本地语义摘要不一致")
    previous_body = {
        key: value for key, value in previous_evidence.items()
        if key != "search_fingerprint"
    }
    if canonical_sha256(previous_body) != previous_evidence.get("search_fingerprint"):
        raise ValueError("旧搜索证据摘要无效")

    source_by_name = {str(source.name): source for source in sources}
    local_by_id = {
        str(item["game_id"]): item for item in analysis.get("games", [])
    }
    games = [
        {
            **game,
            "sources": [dict(report) for report in game.get("sources", [])],
        }
        for game in previous_evidence.get("games", [])
    ]
    targets: list[tuple[dict[str, Any], int, Any, str]] = []
    for game in games:
        game_id = str(game.get("game_id") or "")
        if game_id not in local_by_id or game_id not in semantics_by_id:
            raise ValueError(f"旧搜索证据含未知游戏：{game_id}")
        for source_index, report in enumerate(game.get("sources", [])):
            if not any(_retryable_network_error(item.get("error"))
                       for item in report.get("rounds", [])):
                continue
            source_name = str(report.get("source") or "")
            if source_name not in source_by_name:
                raise ValueError(f"旧搜索证据含未知来源：{source_name}")
            targets.append((game, source_index, source_by_name[source_name], game_id))
    if not targets:
        raise ValueError("旧搜索证据中没有可重试的网络错误")

    with ThreadPoolExecutor(max_workers=min(30, len(targets))) as executor:
        futures = {
            executor.submit(
                _retry_game_source_network_errors,
                game_id,
                semantics_by_id[game_id],
                local_by_id[game_id],
                source,
                serialize_candidate,
                game["sources"][source_index],
            ): (game, source_index, source)
            for game, source_index, source, game_id in targets
        }
        completed = 0
        for future in as_completed(futures):
            game, source_index, source = futures[future]
            game["sources"][source_index] = future.result()
            completed += 1
            if progress is not None:
                progress(f"{source.name} 手动重试", completed, len(targets))

    body = {
        key: value for key, value in previous_evidence.items()
        if key not in {"search_fingerprint", "audit_run_id", "audit_log_dir", "games"}
    }
    body.update({
        "games": games,
        "retry_parent_search_fingerprint": previous_evidence["search_fingerprint"],
        "manual_network_retry_count": (
            int(previous_evidence.get("manual_network_retry_count") or 0) + 1
        ),
    })
    return {**body, "search_fingerprint": canonical_sha256(body)}


def _retry_game_source_network_errors(
    game_id: str,
    semantics: dict[str, Any],
    local: dict[str, Any],
    source: Any,
    serialize_candidate: Callable[[Any], dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    """为统一第二轮提供独立的单游戏、单来源时间预算。"""
    del game_id
    begin_budget = getattr(source, "begin_search_budget", None)
    end_budget = getattr(source, "end_search_budget", None)
    if begin_budget is not None:
        begin_budget()
    try:
        return _retry_network_error_rounds(
            local, semantics, source, serialize_candidate, report
        )
    finally:
        if end_budget is not None:
            end_budget()


def _short_search_game_source(
    semantics: dict[str, Any],
    source: Any,
    serialize_candidate: Callable[[Any], dict[str, Any]],
    report: dict[str, Any],
) -> dict[str, Any]:
    """仅为本来源的空结果尝试 Skill 审定的原名短词。"""
    if report.get("candidates") or any(
        item.get("error") for item in report.get("rounds", [])
    ):
        return report
    titles = semantics.get("titles") or {}
    short_keywords = titles.get("short_search_keywords") or {}
    rounds = [dict(item) for item in report.get("rounds", [])]
    attempts: list[dict[str, Any]] = []
    collected: list[dict[str, Any]] = []
    queried = {
        str(item.get("query") or "").casefold()
        for item in rounds if item.get("query")
    }
    developer = (semantics.get("developer_evidence") or {}).get("value")
    empty_identifiers = {"vndb": [], "dlsite": [], "steam": []}
    begin_budget = getattr(source, "begin_search_budget", None)
    end_budget = getattr(source, "end_search_budget", None)
    if begin_budget is not None:
        begin_budget()
    try:
        for language in ("jp", "zh", "en"):
            query = str(short_keywords.get(language) or "")
            target_round = next(
                (item for item in rounds if item.get("language") == language),
                None,
            )
            if (
                not query or query.casefold() in queried
                or target_round is None
                or target_round.get("status") in {"missing", "covered"}
            ):
                continue
            queried.add(query.casefold())
            attempt: dict[str, Any] = {
                "number": 3, "language": language, "query": query,
            }
            try:
                found = source.search([query], empty_identifiers, developer)
                serialized = [serialize_candidate(item) for item in found]
                attempt.update({
                    "status": "success" if serialized else "no_result",
                    "candidates": serialized,
                    **_cache_provenance(source),
                })
            except Exception as exc:
                serialized = []
                attempt.update({
                    "status": "network_error", "candidates": [],
                    "error": _network_error_text(exc),
                    **_cache_provenance(source),
                })
            attempts.append(attempt)
            target_round.update({
                "fallback_round": 3,
                "fallback_status": attempt["status"],
                "fallback_query": query,
                "discovery_method": "source_short_keyword",
            })
            if attempt.get("error"):
                target_round["fallback_error"] = attempt["error"]
            if serialized:
                target_round["prior_status"] = target_round["status"]
                target_round["status"] = "success"
                target_round["candidates"] = serialized
            if serialized:
                collected.extend(serialized)
                break
    finally:
        if end_budget is not None:
            end_budget()
    return {
        **report,
        "rounds": rounds,
        "candidates": collected,
        **({"fallback_attempts": attempts} if attempts else {}),
    }


def make_analysis_document(
    root: str,
    root_fingerprint: str,
    games: Sequence[dict[str, Any]],
    series: Sequence[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """排序并封装本地证据，同时为完整内容计算输入指纹。"""
    body = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "root": root,
        "root_fingerprint": root_fingerprint,
        "games": sorted(games, key=lambda item: item["relative_path"].casefold()),
        "series": sorted(series, key=lambda item: item["relative_path"].casefold()),
    }
    body.update(extra or {})
    return {**body, "input_fingerprint": canonical_sha256(body)}


def make_search_document(
    analysis: dict[str, Any],
    local_semantics: dict[str, Any],
    semantics_by_id: dict[str, dict[str, Any]],
    sources: Sequence[Any],
    serialize_candidate: Callable[[Any], dict[str, Any]],
    progress: Callable[[str, int, int], None] | None = None,
) -> dict[str, Any]:
    """严格使用 Skill 关键词请求各来源，并保存逐轮原始证据。"""
    game_evidence = {
        item["game_id"]: item for item in analysis.get("games", [])
    }
    ordered = list(semantics_by_id.items())
    reports: dict[tuple[str, int], dict[str, Any]] = {}
    accepted = [item for item in ordered if item[1]["review_state"] == "accepted"]
    if accepted and sources:
        workers = min(30, len(accepted) * len(sources))
        completed_by_source = {index: 0 for index in range(len(sources))}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {}
            for game_id, semantics in accepted:
                for source_index, source in enumerate(sources):
                    future = executor.submit(
                        _search_game_source,
                        game_id,
                        semantics,
                        game_evidence[game_id],
                        source,
                        serialize_candidate,
                    )
                    futures[future] = (game_id, source_index, source)
            for future in as_completed(futures):
                game_id, source_index, source = futures[future]
                reports[(game_id, source_index)] = future.result()
                completed_by_source[source_index] += 1
                if progress is not None:
                    progress(
                        str(source.name), completed_by_source[source_index],
                        len(accepted),
                    )

    # 三来源首轮全部结束后，统一只重试一次网络错误。
    retry_targets = [
        (game_id, source_index, source, semantics)
        for game_id, semantics in accepted
        for source_index, source in enumerate(sources)
        if any(
            _retryable_network_error(item.get("error"))
            for item in reports[(game_id, source_index)].get("rounds", [])
        )
    ]
    if retry_targets:
        with ThreadPoolExecutor(max_workers=min(30, len(retry_targets))) as executor:
            futures = {
                executor.submit(
                    _retry_game_source_network_errors,
                    game_id, semantics, game_evidence[game_id], source,
                    serialize_candidate, reports[(game_id, source_index)],
                ): (game_id, source_index, source)
                for game_id, source_index, source, semantics in retry_targets
            }
            for future in as_completed(futures):
                game_id, source_index, source = futures[future]
                reports[(game_id, source_index)] = future.result()

    # 第三阶段按来源独立补搜 Skill 审定的短词；任何来源均不调用外部搜索引擎。
    short_targets = [
        (game_id, source_index, source, semantics)
        for game_id, semantics in accepted
        for source_index, source in enumerate(sources)
        if not reports[(game_id, source_index)].get("candidates")
        and not any(
            item.get("error")
            for item in reports[(game_id, source_index)].get("rounds", [])
        )
        and any(
            (semantics.get("titles") or {}).get("short_search_keywords", {}).get(
                language
            )
            for language in ("jp", "zh", "en")
        )
    ]
    if short_targets:
        with ThreadPoolExecutor(max_workers=min(30, len(short_targets))) as executor:
            futures = {
                executor.submit(
                    _short_search_game_source,
                    semantics, source, serialize_candidate,
                    reports[(game_id, source_index)],
                ): (game_id, source_index)
                for game_id, source_index, source, semantics in short_targets
            }
            completed = 0
            for future in as_completed(futures):
                game_id, source_index = futures[future]
                reports[(game_id, source_index)] = future.result()
                completed += 1
                if progress is not None:
                    progress("站点短词补搜", completed, len(short_targets))

    games: list[dict[str, Any]] = []
    for game_id, semantics in ordered:
        accepted_game = semantics["review_state"] == "accepted"
        games.append({
            "game_id": game_id,
            "review_state": "accepted" if accepted_game else "needs_user_review",
            "sources": [
                reports[(game_id, source_index)]
                for source_index in range(len(sources))
            ] if accepted_game else [],
        })
    body = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "analysis_fingerprint": analysis["input_fingerprint"],
        "local_semantics_fingerprint": canonical_sha256(local_semantics),
        "games": games,
    }
    return {**body, "search_fingerprint": canonical_sha256(body)}
