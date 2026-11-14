"""Small RU/KK text helpers shared by the AI layer: tokenization, fuzzy stem matching, language detection."""

from __future__ import annotations

import re

KK_LETTERS = set("әғқңөұүһі")
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

STOPWORDS = {
    # ru
    "и",
    "в",
    "во",
    "на",
    "с",
    "со",
    "к",
    "ко",
    "по",
    "за",
    "из",
    "у",
    "о",
    "об",
    "от",
    "до",
    "для",
    "при",
    "а",
    "но",
    "или",
    "ли",
    "же",
    "бы",
    "не",
    "ни",
    "то",
    "это",
    "как",
    "что",
    "чем",
    "так",
    "там",
    "тут",
    "я",
    "мне",
    "меня",
    "мы",
    "вы",
    "вас",
    "вам",
    "ваш",
    "ваша",
    "ваши",
    "он",
    "она",
    "они",
    "его",
    "её",
    "можно",
    "хочу",
    "хотел",
    "хотела",
    "есть",
    "будет",
    "нужно",
    "надо",
    "пожалуйста",
    "здравствуйте",
    "привет",
    "добрый",
    "день",
    "вечер",
    "утро",
    "какая",
    "какой",
    "какие",
    "сколько",
    "стоит",
    "цена",
    "ещё",
    "еще",
    "очень",
    "мой",
    "моя",
    # kk
    "мен",
    "сен",
    "сіз",
    "сізде",
    "біз",
    "бар",
    "ма",
    "ме",
    "ба",
    "бе",
    "па",
    "пе",
    "және",
    "да",
    "де",
    "та",
    "те",
    "керек",
    "қанша",
    "сәлеметсіз",
    "сәлем",
    "рақмет",
    "рахмет",
    "ия",
    "иә",
    "жоқ",
    "бұл",
    "осы",
    "қалай",
    "маған",
}

RU_MARKERS = {
    "здравствуйте",
    "привет",
    "добрый",
    "можно",
    "хочу",
    "записаться",
    "запишите",
    "сколько",
    "спасибо",
    "да",
    "нет",
    "завтра",
    "сегодня",
    "пожалуйста",
    "меня",
    "мне",
    "зовут",
    "есть",
    "это",
    "как",
    "что",
    "на",
    "в",
    "и",
    "не",
    "вы",
    "у",
    "вас",
    "где",
    "когда",
    "время",
    "стрижка",
    "стоит",
    "цена",
    "адрес",
    "отменить",
    "перенести",
    "подтверждаю",
    "хорошо",
    "ладно",
    "давайте",
    "послезавтра",
    "утром",
    "вечером",
}
KK_MARKERS = {
    "сәлем",
    "сәлеметсіз",
    "салем",
    "саламатсызба",
    "ия",
    "иә",
    "рахмет",
    "рақмет",
    "жоқ",
    "бар",
    "ма",
    "ме",
    "керек",
    "кешке",
    "ертең",
    "бүгін",
    "қанша",
    "жазылғым",
    "жазылу",
    "жазыңыз",
    "мен",
    "менің",
    "атым",
    "қай",
    "қайда",
    "уақыт",
    "болады",
    "жақсы",
    "келемін",
    "мекенжай",
    "бүрсігүні",
    "шаш",
}


def tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower().replace("ё", "е"))


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if len(t) >= 3 and t not in STOPWORDS]


def stem_match(a: str, b: str) -> bool:
    """Crude morphology-tolerant match (стрижку~стрижка, қиюға~қиюы)."""
    if a == b:
        return True
    shortest = min(len(a), len(b))
    if shortest < 3:
        return False
    need = 3 if shortest <= 4 else 4
    common = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        common += 1
    return common >= need


def overlap(query_tokens: list[str], target_tokens: list[str]) -> int:
    return sum(1 for q in set(query_tokens) if any(stem_match(q, t) for t in target_tokens))


def detect_language(text: str) -> str | None:
    """Return "kk" / "ru" when the text carries a clear signal, None otherwise (keep current language)."""
    words = tokens(text)
    if not words:
        return None
    kk = sum(1 for w in words if w in KK_MARKERS or any(ch in KK_LETTERS for ch in w))
    ru = sum(1 for w in words if w in RU_MARKERS)
    if kk == 0 and ru == 0:
        cyr = any("а" <= ch <= "я" for w in words for ch in w)
        return "ru" if cyr and len(words) >= 3 else None
    return "kk" if kk > ru else "ru"
