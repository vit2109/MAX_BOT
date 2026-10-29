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

# Дополнительные географические названия, которых может не быть в пользовательской
# группе 4. Список намеренно ограничен хорошо известными населёнными пунктами;
# список регионов из настроек остаётся основным источником географии.
COMMON_RU_PLACES = [
    "Чита", "Забайкальск", "Борзя", "Кировск", "Всеволожск", "Низино",
    "Петербург|Санкт-Петербург", "Санкт-Петербург", "Екатеринбург", "Новосибирск",
    "Красноярск", "Владивосток", "Хабаровск", "Иркутск", "Томск", "Омск",
    "Тюмень", "Курган", "Челябинск", "Пермь", "Уфа", "Казань", "Самара",
    "Саратов", "Волгоград", "Ростов-на-Дону", "Краснодар", "Сочи", "Ставрополь",
    "Воронеж", "Белгород", "Брянск", "Курск", "Орёл", "Тула", "Рязань",
    "Тверь", "Ярославль", "Вологда", "Архангельск", "Мурманск", "Петрозаводск",
    "Псков", "Великий Новгород", "Нижний Новгород", "Киров", "Ижевск", "Оренбург",
    "Пенза", "Ульяновск", "Тольятти", "Астрахань", "Калининград", "Смоленск",
    "Липецк", "Тамбов", "Кострома", "Иваново", "Владимир", "Симферополь",
    "Севастополь", "Луганск", "Донецк", "Мариуполь", "Мелитополь", "Бердянск",
    "Якутск", "Магадан", "Южно-Сахалинск", "Петропавловск-Камчатский", "Благовещенск",
    "Биробиджан", "Нарьян-Мар", "Салехард", "Ханты-Мансийск", "Сургут", "Нижневартовск",
    "Грозный", "Махачкала", "Владикавказ", "Нальчик", "Черкесск", "Элиста", "Майкоп",
    "Абакан", "Кызыл", "Горно-Алтайск", "Барнаул", "Кемерово", "Новокузнецк",
    "Калуга", "Сасово", "Подольск", "Химки", "Мытищи", "Королёв", "Балашиха", "Одинцово",
]

@dataclass
class KeywordGroup:
    id: int
    name: str
    role: str
    keywords: list[str] = field(default_factory=list)

@dataclass
class MatchResult:
    matched: bool
    sendable: bool
    severity: str
    labels: list[str]
    hits_by_group: dict[int, list[str]]
    all_hits: list[str]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower().replace("ё", "е")


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
        by_id[2].keywords = _unique(fallback_keywords)
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
    """Classify actual telecom infrastructure incidents in Russia and separate concrete threats.

    Keywords are supporting evidence, not sufficient conditions. The relevant event clause
    must describe a concrete service impact / telecom-infrastructure damage or a specific
    prospective threat, and the same clause must identify Russian territory.
    """
    raw_text = str(text or "")
    clauses = _split_clauses(raw_text)
    group_map = {group.id: group for group in groups}
    topic_words = group_map.get(1, KeywordGroup(1, "Тематика", "topic")).keywords
    neg_words = group_map.get(2, KeywordGroup(2, "Негатив", "critical")).keywords
    active_words = group_map.get(3, KeywordGroup(3, "Статус события", "active")).keywords
    geo_words = _unique([*(group_map.get(4, KeywordGroup(4, "Топоним", "toponym")).keywords), *COMMON_RU_PLACES])
    recovery_words = group_map.get(5, KeywordGroup(5, "Снято / устранено", "resolved")).keywords

    matched_clause: str | None = None
    matched_kind: str | None = None  # incident / warning / recovery
    matched_geo: list[str] = []
    relevant_hits: dict[int, list[str]] = {}

    all_geo = False
    for clause in clauses:
        norm = normalize_text(clause)
        if not norm:
            continue
        geo_hits = _find_toponyms(clause, geo_words)
        territory = _has_russian_location(clause, geo_hits)
        if not territory:
            continue

        topic_hits = find_keywords(clause, topic_words)
        neg_hits = find_keywords(_without_negated_signals(clause), neg_words)
        active_hits = find_keywords(_without_negated_signals(clause), active_words)
        recovery_hits = find_keywords(_confirmed_recovery_text(clause), recovery_words)
        has_telecom_context = bool(topic_hits) or bool(_TELECOM_CONTEXT.search(norm))
        if not has_telecom_context:
            continue

        # Planned maintenance and general proposals are not incidents.
        planned = bool(_PLANNED.search(norm))
        threat = _is_immediate_threat_clause(clause)
        confirmed_impact = _has_concrete_service_impact(clause)
        physical_damage = _has_telecom_infrastructure_damage(clause)
        recovery = bool(recovery_hits and _has_recovery_context(clause))

        kind = None
        if threat and not confirmed_impact and not physical_damage:
            kind = "warning"
        elif recovery and not planned:
            kind = "recovery"
        elif (confirmed_impact or physical_damage) and not planned and not _is_hypothetical_only(norm):
            kind = "incident"

        if kind is None:
            continue

        clause_hits = _unique([*topic_hits, *neg_hits, *active_hits, *geo_hits, *recovery_hits])
        if len(clause_hits) < max(1, int(min_hits or 1)):
            # Keep min_hits as an additional filter, but direct service-impact evidence
            # is still required even if many irrelevant terms occur elsewhere.
            continue
        matched_clause = clause
        matched_kind = kind
        matched_geo = geo_hits
        relevant_hits = {}
        if topic_hits: relevant_hits[1] = topic_hits
        if neg_hits: relevant_hits[2] = neg_hits
        if active_hits: relevant_hits[3] = active_hits
        if geo_hits: relevant_hits[4] = geo_hits
        if recovery_hits: relevant_hits[5] = recovery_hits
        break

    # A narrowly defined recurring outage report may spread the provider's location and
    # the outage statement across separate sentences (for example, “provider of Petersburg”
    # followed by “complaints about outages recur / problems have continued since September”).
    # Accept only a specific ongoing/recurrent outage statement, never generic “problems”.
    if matched_clause is None:
        recurring = _has_recurrent_local_provider_issue(raw_text, geo_words, topic_words)
        if recurring:
            matched_clause = recurring
            matched_kind = "recurrent"
            matched_geo = _find_toponyms(raw_text, geo_words)
            relevant_hits = {}
            local_topic = find_keywords(raw_text, topic_words)
            local_negative = find_keywords(_without_negated_signals(raw_text), neg_words)
            local_active = find_keywords(_without_negated_signals(raw_text), active_words)
            if local_topic: relevant_hits[1] = local_topic
            if local_negative: relevant_hits[2] = local_negative
            if local_active: relevant_hits[3] = local_active
            if matched_geo: relevant_hits[4] = matched_geo

    # Return broader keyword hits for debugging, but decision is based on a local event clause.
    hits_by_group: dict[int, list[str]] = {}
    for group in groups:
        if group.id == 4:
            hits = _find_toponyms(raw_text, group.keywords)
        elif group.id == 2:
            hits = find_keywords(_without_negated_signals(raw_text), group.keywords)
        elif group.id == 3:
            hits = find_keywords(_without_negated_signals(raw_text), group.keywords)
        elif group.id == 5:
            hits = find_keywords(_confirmed_recovery_text(raw_text), group.keywords)
        else:
            hits = find_keywords(raw_text, group.keywords)
        if hits:
            hits_by_group[group.id] = hits

    labels: list[str] = []
    severity = "none"
    matched = bool(matched_clause)
    sendable = False
    if matched and matched_kind == "warning":
        severity = "warning"
        labels = ["угроза", "территория РФ"]
        if matched_geo: labels.append("топоним")
    elif matched and matched_kind == "recovery":
        severity = "normal"
        labels = ["нормальное", "территория РФ"]
    elif matched and matched_kind in {"incident", "recurrent"}:
        clause = normalize_text(matched_clause or "")
        if matched_kind == "recurrent":
            severity = "active"
        else:
            severity = "critical" if _SEVERE.search(clause) or find_keywords(clause, neg_words) else "active"
        labels = ["критическое" if severity == "critical" else "активное", "территория РФ"]
        sendable = severity in {"critical", "active"}
        if matched_geo: labels.append("топоним")

    # To keep match counts and debug display aligned, expose local evidence first.
    if matched and relevant_hits:
        hits_by_group = {gid: _unique([*relevant_hits.get(gid, []), *hits_by_group.get(gid, [])])
                         for gid in sorted(set(relevant_hits) | set(hits_by_group))
                         if relevant_hits.get(gid) or hits_by_group.get(gid)}
    all_hits = _unique(word for found in hits_by_group.values() for word in found)
    return MatchResult(matched, sendable, severity, labels, hits_by_group, all_hits)


# Concrete service impact phrases. Generic mention of "сбой", "проблема", "жалобы" or
# DDoS statistics alone intentionally does not satisfy this test.
_TELECOM_CONTEXT = re.compile(
    r"\b(?:связ\w*|интернет\w*|телеком\w*|оператор\w*|провайдер\w*|кабел\w*|волс|волп|"
    r"базов\w*\s+станц\w*|узел\w*\s+связ\w*|лини\w*\s+связ\w*|"
    r"телефонн\w*\s+компан\w*|телефони\w*|сайт\w*|сеть\w*|сигнал\w*|"
    r"звонк\w*|услуг\w*\s+связ\w*|цод|дата-центр\w*)\b"
)

_SERVICE_IMPACT = re.compile(
    r"\b(?:нет\s+(?:мобильн\w*\s+|фиксированн\w*\s+)?(?:связ\w*|интернет\w*|сигнал\w*)|"
    r"отсутств\w*\s+(?:связ\w*|интернет\w*|сигнал\w*)|"
    r"пропал\w*\s+(?:мобильн\w*\s+)?(?:связ\w*|интернет\w*|сигнал\w*)|"
    r"исчез\w*\s+(?:мобильн\w*\s+)?(?:связ\w*|интернет\w*|сигнал\w*)|"
    r"перебо\w*[^.!?;]{0,65}(?:связ\w*|интернет\w*|звонк\w*|сигнал\w*)|"
    r"(?:связ\w*|интернет\w*|звонк\w*|сигнал\w*)[^.!?;]{0,65}перебо\w*|"
    r"(?:связ\w*|интернет\w*|сайт\w*|звонк\w*|сигнал\w*)[^.!?;]{0,50}не\s+работа\w*|"
    r"не\s+работа\w*[^.!?;]{0,50}(?:связ\w*|интернет\w*|сайт\w*|сигнал\w*|сеть\w*)|"
    r"не\s+проход\w*[^.!?;]{0,50}звонк\w*|"
    r"не\s+ловит\w*[^.!?;]{0,50}(?:связ\w*|сигнал\w*|сеть\w*)|"
    r"недоступ\w*[^.!?;]{0,50}(?:связ\w*|интернет\w*|сеть\w*|сайт\w*)|"
    r"(?:абонент\w*|пользоват\w*)[^.!?;]{0,80}(?:не\s+могут\s+(?:позвонить|выйти\s+в\s+интернет)|остал\w*\s+без\s+(?:связ\w*|интернет\w*))|"
    r"(?:отключен\w*|отключил\w*)[^.!?;]{0,50}(?:связ\w*|интернет\w*|сеть\w*))\b"
)

_INFRA_DAMAGE = re.compile(
    # Explicitly confirmed physical damage / fire / hit to telecom infrastructure.
    r"\b(?:поврежд\w*|обрыв\w*|сгорел\w*|разрушен\w*|уничтожен\w*|"
    r"взрыв\w*|пожар\w*|поражен\w*)[^.!?;]{0,100}"
    r"(?:кабел\w*\s+связ\w*|волс|волп|лини\w*\s+связ\w*|узел\w*\s+связ\w*|"
    r"базов\w*\s+станц\w*|телефонн\w*\s+компан\w*|объект\w*\s+связ\w*|"
    r"телеком\w*\s+инфраструктур\w*|оборудован\w*\s+связ\w*|цод|дата-центр\w*|центр\w*\s+обработки\s+данных)\b|"
    r"\b(?:кабел\w*\s+связ\w*|волс|волп|лини\w*\s+связ\w*|узел\w*\s+связ\w*|"
    r"базов\w*\s+станц\w*|телефонн\w*\s+компан\w*|объект\w*\s+связ\w*|цод|дата-центр\w*|центр\w*\s+обработки\s+данных)[^.!?;]{0,100}"
    r"(?:поврежд\w*|обрыв\w*|сгорел\w*|разрушен\w*|уничтожен\w*|взрыв\w*|пожар\w*)\b|"
    # “Под удар попало здание телефонной компании” is a concrete impact, unlike a generic mention of an attack.
    r"\bпод\s+удар\s+попал\w*[^.!?;]{0,90}(?:кабел\w*\s+связ\w*|волс|волп|лини\w*\s+связ\w*|"
    r"узел\w*\s+связ\w*|базов\w*\s+станц\w*|телефонн\w*\s+компан\w*|объект\w*\s+связ\w*|"
    r"цод|дата-центр\w*|центр\w*\s+обработки\s+данных)\b"
)

_THREAT_MARKER = re.compile(
    r"\b(?:возможн\w*|может\s+(?:пропаст\w*|привест\w*|наруш\w*|повред\w*|останов\w*|затрон\w*)|"
    r"могут\s+(?:повред\w*|наруш\w*|привест\w*|стать\s+цел\w*)|"
    r"угроз\w*|риск\w*|опаса\w*|под\s+угроз\w*|план\w*\s+атак\w*|"
    r"может\s+стать\s+цел\w*|предупрежда\w*)\b"
)
_PLANNED = re.compile(r"\b(?:планов\w*\s+работ\w*|техническ\w*\s+работ\w*|профилактик\w*|запланир\w*|предстоит\s+отключ\w*)\b")
_SEVERE = re.compile(r"\b(?:авари\w*|поврежд\w*|обрыв\w*|пожар\w*|взрыв\w*|атак\w*|удар\w*|поражен\w*|разрушен\w*)\b")
_HYPOTHETICAL_ONLY = re.compile(
    r"\b(?:возможн\w*|может|могут|могло|могла|могли|при\s+условии|в\s+случае|"
    r"если\s+(?:произойдет|случится|будет)|планиру\w*|предлага\w*|предполага\w*)\b"
)


def _has_recurrent_local_provider_issue(text: str, geo_words: list[str], topic_words: list[str]) -> str | None:
    norm = normalize_text(text)
    # Require both a recurrence/persistence statement and an explicit network/provider context.
    recurrence_patterns = (
        r"жалоб\w*[^.!?;]{0,60}на\s+(?:регулярн\w*\s+)?сбо\w*[^.!?;]{0,50}(?:появля\w*|поступа\w*|повторя\w*)",
        r"(?:проблем\w*|сбо\w*|перебо\w*)\s+у\s+[а-яa-z0-9«»\" -]{2,60}\s+(?:ид\w*|продолжа\w*|сохраня\w*)[^.!?;]{0,60}(?:с\s+(?:сентябр\w*|октябр\w*|август\w*|июл\w*|июн\w*|ма\w*|апрел\w*|март\w*|феврал\w*|январ\w*|\d{1,2}[.]?\d{0,2})|периодическ\w*|регулярн\w*)",
        r"(?:регулярн\w*|периодическ\w*)[^.!?;]{0,50}(?:сбо\w*|перебо\w*)[^.!?;]{0,50}(?:связ\w*|интернет\w*|сет\w*|оператор\w*|провайдер\w*)",
    )
    if not any(re.search(pattern, norm) for pattern in recurrence_patterns):
        return None
    if not find_keywords(text, topic_words) and not _TELECOM_CONTEXT.search(norm):
        return None
    geo_hits = _find_toponyms(text, geo_words)
    if not _has_russian_location(text, geo_hits):
        return None
    return text


def _has_concrete_service_impact(clause: str) -> bool:
    norm = normalize_text(clause)
    if not _SERVICE_IMPACT.search(norm):
        return False
    # A potential/conditional impact must be classified as a threat, not a confirmed outage.
    if _THREAT_MARKER.search(norm):
        return False
    return True


def _has_telecom_infrastructure_damage(clause: str) -> bool:
    return bool(_INFRA_DAMAGE.search(normalize_text(clause)))


def _is_immediate_threat_clause(clause: str) -> bool:
    norm = normalize_text(clause)
    return bool(_THREAT_MARKER.search(norm) and _TELECOM_CONTEXT.search(norm) and
                (_SERVICE_IMPACT.search(norm) or re.search(r"\b(?:кабел\w*|волс|волп|базов\w*\s+станц\w*|узел\w*\s+связ\w*)\b", norm)))


def _has_recovery_context(clause: str) -> bool:
    norm = normalize_text(clause)
    return bool(re.search(r"\b(?:связ\w*|интернет\w*|сеть\w*|услуг\w*\s+связ\w*)\b", norm) and
                re.search(r"\b(?:после\s+(?:авари\w*|сбо\w*|поврежд\w*|обрыв\w*)|авари\w*|сбо\w*|перебо\w*|поврежд\w*)\b", norm))


def _is_hypothetical_only(norm: str) -> bool:
    # The mere presence of a future marker should not erase an independently reported impact.
    if _THREAT_MARKER.search(norm):
        return True
    return bool(re.search(r"\b(?:планов\w*|профилактик\w*|законопроект\w*|предлага\w*|обсужда\w*|может\s+быть)\b", norm))


def _split_clauses(text: str) -> list[str]:
    # Keep commas and colons inside a clause to preserve local connection between location and event.
    return [part.strip() for part in re.split(r"[\n.!?;]+", str(text or "")) if part.strip()]


def _has_russian_location(clause: str, toponym_hits: list[str]) -> bool:
    norm = normalize_text(clause)
    explicit = bool(re.search(r"\b(?:росси\w*|рф|российск\w*|на\s+территории\s+росси\w*)\b", norm))
    return explicit or bool(toponym_hits)


def _find_toponyms(text: str, keywords: list[str]) -> list[str]:
    found = find_keywords(text, keywords)
    normalized = normalize_text(text)
    seen = {normalize_text(item) for item in found}
    for keyword in keywords:
        for variant in _variants(keyword):
            if normalize_text(keyword) in seen or not variant or " " in variant:
                continue
            if not re.fullmatch(r"[а-я-]+", variant):
                continue
            stem = variant
            for ending in ("ия", "ье", "ово", "ево", "ино", "а", "я", "ь"):
                if variant.endswith(ending) and len(variant) - len(ending) >= 3:
                    stem = variant[:-len(ending)]
                    break
            if stem != variant and re.search(r"(?<!\w)" + re.escape(stem) + r"[а-я]{0,4}(?!\w)", normalized):
                found.append(keyword)
                seen.add(normalize_text(keyword))
                break
    return found


def _without_negated_signals(text: str) -> str:
    text = normalize_text(text)
    return re.sub(r"\b(?:без\s+(?:сбо\w*|авари\w*|проблем\w*|перебо\w*)|"
                  r"(?:сбо\w*|авари\w*|проблем\w*|перебо\w*)\s+(?:не\s+(?:было|выявлено|зафиксировано)|нет))", " ", text)


def _confirmed_recovery_text(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"\b(?:не|будет|будут|планируется|обещают|планируют|чтобы|если|после)\s+"
                  r"(?:(?:еще|пока|полностью|был|была|были|связь|работа|интернет)\s+){0,3}"
                  r"(?:восстанов\w*|устран\w*|ликвидир\w*|нормализ\w*)", " ", text)
    text = re.sub(r"\b(?:восстановить|устранить|ликвидировать|нормализовать|восстановлени\w*|устранени\w*|ликвидаци\w*|нормализаци\w*)", " ", text)
    return text


def _unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        key = normalize_text(item)
        if key not in seen:
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
    if len(word) < 5 or re.search(r"[\d\-]", word) or not re.fullmatch(r"[а-я]+", word):
        return re.escape(word)
    stem = word
    for ending in ("ая", "яя", "ое", "ее", "ые", "ие", "ий", "ый", "ой", "ия", "а", "я", "ы", "и", "ь"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            stem = word[:-len(ending)]
            break
    return re.escape(stem) + r"[а-я]{0,5}"


def preview_match(text: str, groups: list[KeywordGroup], min_hits: int = 1, *,
                  send_enabled: bool = False, send_only_critical: bool = False,
                  has_recipients: bool = False) -> dict:
    result = match_message(text, groups, min_hits)
    would_send = bool(result.matched and result.sendable and send_enabled and has_recipients and
                       not (send_only_critical and result.severity != "critical"))
    if result.severity == "warning":
        filtered_reason = "Отдельная категория «Угрозы»: конкретная потенциальная проблема связи в РФ, но подтверждённого нарушения пока нет."
    elif result.matched and result.severity == "normal":
        filtered_reason = "Релевантное сообщение о восстановлении связи в РФ после аварии."
    elif result.matched:
        filtered_reason = "Есть признаки фактического нарушения связи или повреждения телеком-инфраструктуры в РФ."
    else:
        filtered_reason = "Отсеяно: не найдено достаточного подтверждения реальной проблемы связи / инфраструктуры в том же контексте с указанием территории РФ."
    if would_send:
        send_reason = "Уйдёт адресатам (отправка включена, адресаты заданы)."
    elif result.matched and result.severity == "warning":
        send_reason = "Не отправляется как авария: угроза показывается отдельно."
    elif result.matched:
        send_reason = "В результатах тестирования; фактическая отправка не выполнялась."
    else:
        send_reason = "Не отправляется, так как сообщение отфильтровано."
    names = {group.id: group.name for group in groups}
    return {"matched": result.matched, "would_send": would_send, "sendable": result.sendable,
            "severity": result.severity, "labels": result.labels,
            "hits": {f"{gid}. {names.get(gid, gid)}": words for gid, words in result.hits_by_group.items()},
            "all_hits": result.all_hits, "filtered_reason": filtered_reason, "send_reason": send_reason}
