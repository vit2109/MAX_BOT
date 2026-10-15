from __future__ import annotations

import re
from dataclasses import dataclass, field

GROUP_DEFS = (
    (1, "Тематика", "topic"),
    (2, "Негатив", "critical"),
    (3, "Статус события", "active"),
    (4, "Топоним", "toponym"),
    (5, "Снято / устранено", "resolved"),
)


@dataclass
class KeywordGroup:
    id: int
    name: str
    role: str
    keywords: list[str] = field(default_factory=list)


@dataclass
class MatchResult:
    matched: bool
    severity: str
    labels: list[str]
    hits_by_group: dict[int, list[str]]
    all_hits: list[str]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower().replace("ё", "е")


def default_groups() -> list[KeywordGroup]:
    return [KeywordGroup(gid, name, role) for gid, name, role in GROUP_DEFS]


def groups_from_payload(raw_groups: list[dict] | None, fallback_keywords: list[str] | None = None) -> list[KeywordGroup]:
    by_id = {group.id: group for group in default_groups()}
    for item in raw_groups or []:
        try:
            gid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if gid not in by_id:
            continue
        by_id[gid].keywords = _unique([str(word) for word in item.get("keywords") or []])
        if item.get("name"):
            by_id[gid].name = str(item["name"])
        if item.get("role"):
            by_id[gid].role = str(item["role"])
    groups = [by_id[gid] for gid, _, _ in GROUP_DEFS]
    if not any(group.keywords for group in groups) and fallback_keywords:
        groups[1].keywords = _unique(fallback_keywords)
    return groups


def flatten_keywords(groups: list[KeywordGroup]) -> list[str]:
    return _unique(word for group in groups for word in group.keywords)


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    haystack = normalize_text(text)
    found: list[str] = []
    seen: set[str] = set()
    for keyword in keywords:
        for variant in _variants(keyword):
            if variant in seen:
                continue
            if _contains(haystack, variant):
                found.append(keyword)
                seen.add(variant)
                break
    return found


def match_message(text: str, groups: list[KeywordGroup], min_hits: int = 1) -> MatchResult:
    """Require telecom context and an incident/recovery signal, never geography alone."""
    hits_by_group: dict[int, list[str]] = {}
    for group in groups:
        searchable = _without_negated_signals(text) if group.id in {2, 3} else text
        if group.id == 5:
            searchable = _confirmed_recovery_text(text)
        hits = find_keywords(searchable, group.keywords)
        if hits:
            hits_by_group[group.id] = hits
    all_hits = _unique(word for hits in hits_by_group.values() for word in hits)
    topic = hits_by_group.get(1, [])
    negative = hits_by_group.get(2, [])
    active = hits_by_group.get(3, [])
    toponym = hits_by_group.get(4, [])
    resolved = hits_by_group.get(5, [])
    has_topic_list = bool(next((group.keywords for group in groups if group.id == 1), []))
    signal = negative or active or resolved
    if has_topic_list:
        matched = bool(topic and signal)
    else:
        matched = bool(signal)
    if matched and len(all_hits) < min_hits:
        matched = False
    labels: list[str] = []
    severity = "none"
    if matched:
        normalized = normalize_text(text)
        unresolved = bool(re.search(
            r"(?:не\s+(?:(?:был[аи]?|еще|пока|полностью)\s+){0,3}(?:восстанов\w*|устран\w*|работ\w*)"
            r"|(?:сохраня\w*|продолжа\w*|остаю\w*|остал\w*)[^.!?;]{0,60}(?:сбо\w*|проблем\w*|перебо\w*|недоступ\w*)"
            r"|(?:сбо\w*|проблем\w*|перебо\w*)[^.!?;]{0,60}(?:сохраня\w*|продолжа\w*)"
            r"|частич\w*|не\s+у\s+всех|по-прежнему)", normalized))
        # A recovery in one sentence must not hide an independent outage in another.
        for sentence in re.split(r"[.!?;\n]+|\b(?:но|однако)\b", text, flags=re.I):
            has_recovery = any(find_keywords(_confirmed_recovery_text(sentence), g.keywords)
                               for g in groups if g.id == 5)
            has_incident = any(find_keywords(_without_negated_signals(sentence), g.keywords)
                              for g in groups if g.id in {2, 3})
            historical = bool(re.search(r"\b(?:был\w*|произош\w*|ранее|вчера)\b", normalize_text(sentence)))
            if has_incident and not has_recovery and not historical:
                unresolved = True
        planned = bool(re.search(r"планов\w*|профилакти\w*|техническ\w*\s+работ\w*", normalized))
        severe_incident = bool(re.search(r"\b(?:авари\w*|обрыв\w*|поврежд\w*|пожар\w*|взрыв\w*)", _without_negated_signals(text)))
        if resolved and not unresolved:
            labels.append("нормальное")
            severity = "normal"
        elif negative and (not planned or severe_incident):
            labels.append("критическое")
            severity = "critical"
        elif active or negative or (resolved and unresolved):
            labels.append("активное")
            severity = "active"
        else:
            labels.append("тематическое")
            severity = "topic"
        if active and "активное" not in labels:
            labels.append("активное")
        if toponym:
            labels.append("топоним")
        if planned:
            labels.append("плановые работы")
        if negative and "критическое" not in labels and severity not in {"normal", "active"}:
            labels.append("критическое")
    return MatchResult(
        matched=matched,
        severity=severity,
        labels=labels,
        hits_by_group=hits_by_group,
        all_hits=all_hits,
    )


def preview_match(
    text: str,
    groups: list[KeywordGroup],
    min_hits: int = 1,
    *,
    send_enabled: bool = False,
    send_only_critical: bool = False,
    has_recipients: bool = False,
) -> dict:
    result = match_message(text, groups, min_hits)
    topic = result.hits_by_group.get(1, [])
    negative = result.hits_by_group.get(2, [])
    active = result.hits_by_group.get(3, [])
    toponym = result.hits_by_group.get(4, [])
    resolved = result.hits_by_group.get(5, [])
    has_topic_list = bool(next((group.keywords for group in groups if group.id == 1), []))
    signal = negative or active or resolved
    missing: list[str] = []
    if not (text or "").strip():
        missing.append("текст пустой")
    elif has_topic_list and not topic:
        missing.append("нет слова из группы 1 (тематика)")
    if has_topic_list and topic and not signal:
        missing.append("есть тематика, но нет сообщения о проблеме или восстановлении (группы 2, 3, 5)")
    if not has_topic_list and not result.all_hits:
        missing.append("нет ни одного ключевого слова")
    if topic and signal and len(result.all_hits) < min_hits:
        missing.append(f"совпало слов {len(result.all_hits)}, нужно минимум {min_hits}")
    would_send = bool(
        result.matched
        and send_enabled
        and has_recipients
        and not (send_only_critical and result.severity != "critical")
    )
    send_block: list[str] = []
    if result.matched and not would_send:
        if not send_enabled:
            send_block.append("выключена отправка")
        if not has_recipients:
            send_block.append("пустой список адресатов")
        if send_only_critical and result.severity != "critical":
            send_block.append("включено «только критические», а важность не критическая")
    names = {group.id: group.name for group in groups}
    hits = {
        f"{gid}. {names.get(gid, gid)}": words
        for gid, words in result.hits_by_group.items()
    }
    if result.matched:
        filtered_reason = (
            "Попадёт в «Отфильтрованные»: есть тематика (группа 1) "
            "и сигнал о проблеме или восстановлении (группы 2, 3, 5)."
        )
        if result.severity == "normal":
            filtered_reason += " Подтверждено восстановление без признаков продолжающейся проблемы."
    else:
        filtered_reason = "Не попадёт в «Отфильтрованные»: " + (
            "; ".join(missing) or "условие групп не выполнено"
        )
    if would_send:
        send_reason = "Уйдёт адресатам (отправка включена, адресаты заданы)."
    elif result.matched:
        send_reason = "В отфильтрованные попадёт, адресатам нет: " + (
            "; ".join(send_block) or "отправка не выполняется"
        )
    else:
        send_reason = "Адресатам не уйдёт, потому что сообщение не отфильтровано."
    return {
        "matched": result.matched,
        "would_send": would_send,
        "severity": result.severity,
        "labels": result.labels,
        "hits": hits,
        "all_hits": result.all_hits,
        "filtered_reason": filtered_reason,
        "send_reason": send_reason,
    }


def _unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        key = normalize_text(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _variants(keyword: str) -> list[str]:
    return [normalize_text(part) for part in str(keyword).split("|") if part.strip()]


def _contains(haystack: str, needle: str) -> bool:
    if not needle or not re.search(r"\w", needle):
        return False
    pattern = r"\s+".join(_word_pattern(word) for word in needle.split())
    return re.search(r"(?<!\w)" + pattern + r"(?!\w)", haystack) is not None


def _word_pattern(word: str) -> str:
    if word in {"сбой", "сбои"}:
        return r"сбо(?:й|и|я|ю|е|ев|ем|ям|ями|ях)"
    if len(word) < 5 or re.search(r"[\d\-]", word):
        return re.escape(word)
    if not re.fullmatch(r"[а-я]+", word):
        return re.escape(word)
    stem = word
    for ending in ("ая", "яя", "ое", "ее", "ые", "ие", "ий", "ый", "ой", "ия", "ие", "а", "я", "ы", "и", "ь"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            stem = word[:-len(ending)]
            break
    return re.escape(stem) + r"[а-я]{0,5}"


def _without_negated_signals(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\bбез\s+(?:сбо\w*|авари\w*|проблем\w*|перебо\w*)", " ", text)
    text = re.sub(r"\b(?:сбо\w*|авари\w*|проблем\w*|перебо\w*)\s+(?:не\s+(?:было|выявлено|зафиксировано)|нет)", " ", text)
    return text


def _confirmed_recovery_text(text: str) -> str:
    text = normalize_text(text)
    # Exclude negated, future and hypothetical recovery claims.
    text = re.sub(r"\b(?:не|будет|будут|планируется|обещают|планируют|чтобы|если|после)\s+"
                  r"(?:(?:еще|пока|полностью|был|была|были|связь|работа|интернет)\s+){0,3}"
                  r"(?:восстанов\w*|устран\w*|ликвидир\w*|нормализ\w*)", " ", text)
    text = re.sub(r"\b(?:восстановить|устранить|ликвидировать|нормализовать)\b", " ", text)
    # Nouns describe work or intent; they do not confirm completed recovery.
    text = re.sub(r"\b(?:восстановлени\w*|устранени\w*|ликвидаци\w*|нормализаци\w*)", " ", text)
    return text
