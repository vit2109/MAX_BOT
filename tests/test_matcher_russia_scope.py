import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from matcher import groups_from_payload, match_message, preview_match


def groups():
    return groups_from_payload([
        {"id": 1, "keywords": ["интернет", "оператор связи", "связь", "кабель связи", "мобильная связь"]},
        {"id": 2, "keywords": ["сбой", "авария", "повреждение", "отключение", "перебои"]},
        {"id": 3, "keywords": ["пропала", "не работает", "произошло", "остановилась"]},
        {"id": 4, "keywords": ["Чита", "Москва", "Россия", "России", "РФ"]},
        {"id": 5, "keywords": ["восстановлена", "устранена"]},
    ])


def test_editorial_about_anti_fraud_is_not_an_incident():
    text = "В России обсуждают закон об антифроде. Операторы связи спорят о возможных ограничениях интернета."
    result = match_message(text, groups())
    assert not result.matched
    assert not result.sendable


def test_foreign_cable_threat_is_outside_scope():
    text = "Возможное повреждение кабелей связи в Ормузском проливе может нарушить интернет."
    result = match_message(text, groups())
    assert not result.matched
    assert not result.sendable


def test_threat_in_russia_is_visible_as_warning_but_not_sendable():
    text = "В России существует угроза повреждения кабеля связи, что может нарушить интернет."
    result = match_message(text, groups())
    assert result.matched
    assert result.severity == "warning"
    assert "угроза" in result.labels
    assert not result.sendable


def test_confirmed_incident_in_russia_is_matched():
    text = "В Чите произошёл сбой: пропала мобильная связь и не работает интернет."
    result = match_message(text, groups())
    assert result.matched
    assert result.severity in {"critical", "active"}
    assert result.sendable


def test_power_outage_without_telecom_impact_is_not_matched():
    text = "В Чите произошло отключение электричества, остановились троллейбусы и не работают светофоры."
    result = match_message(text, groups())
    assert not result.matched
    assert not result.sendable


def test_preview_explains_warning_separately():
    text = "В России существует угроза повреждения кабеля связи, что может нарушить интернет."
    result = preview_match(text, groups(), send_enabled=True, has_recipients=True)
    assert result["matched"]
    assert result["severity"] == "warning"
    assert "Угрозы" in result["filtered_reason"]
    assert not result["would_send"]
