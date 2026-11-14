"""Deterministic rule-based agent with the same tool contract as the real LLM.

Good enough for demos and end-to-end tests: RU/KK replies, intents (greeting / price / address / book /
cancel / reschedule / unknown), relative dates (сегодня/завтра/бүгін/ертең/...), times ("16:00", "в 16"),
ambiguous date clarification, booking confirmation only after create_booking succeeded, escalation when
the answer is not in the catalogue or knowledge base. Dialogue slots live in `req.state`.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from app.ai.llm import LLMRequest, LLMResult, ToolExecutor
from app.ai.text import content_tokens, overlap, tokens
from app.ai.tools import TAG_ASKED_PRICE, TAG_NO_SLOT

YES = {
    "да",
    "ага",
    "конечно",
    "подтверждаю",
    "верно",
    "ок",
    "ok",
    "окей",
    "давайте",
    "хорошо",
    "иә",
    "ия",
    "йә",
    "жарайды",
    "болады",
    "растаймын",
    "дұрыс",
    "yes",
}
NO = {"нет", "неа", "жоқ", "no"}

KW_CANCEL = ("отмен", "отказ", "не приду", "бас тарт", "болдырма", "тоқтат")
KW_RESCHEDULE = ("перенес", "перенос", "ауыстыр", "басқа уақыт", "басқа күн")
KW_DISCOUNT = ("скидк", "акци", "жеңілдік", "промокод", "дешевле")
KW_PRICE = ("цен", "стоит", "стоимост", "сколько", "прайс", "баға", "қанша", "құны")
KW_ADDRESS = ("адрес", "где вы", "где наход", "как добраться", "мекенжай", "қайда")
KW_BOOK = ("запис", "запиш", "свобод", "окошк", "окно", "жазыл", "бос орын", "бос уақыт", "жазып")
KW_THANKS = ("спасибо", "благодар", "рахмет", "рақмет")
KW_GREETING = ("здравств", "привет", "добрый", "сәлем", "салем", "саламатсыз", "сәлеметсіз", "қайырлы")
AMBIGUOUS_DATE = (
    "выходн",
    "на неделе",
    "следующей неделе",
    "след неделе",
    "начале недели",
    "конце недели",
    "как-нибудь",
    "на днях",
    "попозже",
    "когда-нибудь",
    "демалыс",
    "келесі апта",
    "апта соңында",
    "аптаның",
)
MONTHS_RU = (
    "январ",
    "феврал",
    "март",
    "апрел",
    "ма",
    "июн",
    "июл",
    "август",
    "сентябр",
    "октябр",
    "ноябр",
    "декабр",
)
MONTHS_KK = (
    "қаңтар",
    "ақпан",
    "наурыз",
    "сәуір",
    "мамыр",
    "маусым",
    "шілде",
    "тамыз",
    "қыркүйек",
    "қазан",
    "қараша",
    "желтоқсан",
)
WEEKDAYS = (
    ("понедельн", "дүйсенбі"),
    ("вторник", "сейсенбі"),
    ("сред", "сәрсенбі"),
    ("четверг", "бейсенбі"),
    ("пятниц", "жұма"),
    ("суббот", "сенбі"),
    ("воскресен", "жексенбі"),
)
WEEKDAY_NAMES = {
    "ru": ("понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"),
    "kk": ("дүйсенбі", "сейсенбі", "сәрсенбі", "бейсенбі", "жұма", "сенбі", "жексенбі"),
}

_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_HOUR_RE = re.compile(
    r"(?:\b(?:в|к|на|сағат)\s+)(\d{1,2})(?![\d.:/])(?!\s*(?:" + "|".join(MONTHS_RU) + r"))", re.IGNORECASE
)
_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?!\d)")
_DAY_MONTH_RE = re.compile(r"(?<!\d)(\d{1,2})\s+([^\W\d_]+)")

T: dict[str, dict[str, str]] = {
    "greeting": {
        "ru": "Здравствуйте! Я виртуальный помощник {tenant}. Подскажу цены и адрес, запишу на удобное время. "
        "Чем могу помочь?",
        "kk": "Сәлеметсіз бе! Мен {tenant} виртуалды көмекшісімін. Бағаларды айтып, ыңғайлы уақытқа жазып "
        "бере аламын. Қалай көмектесейін?",
    },
    "ask_service": {
        "ru": "На какую услугу вас записать? Доступно: {services}.",
        "kk": "Қай қызметке жазылғыңыз келеді? Қолжетімді: {services}.",
    },
    "ask_date": {
        "ru": "На какой день вас записать? Например: сегодня, завтра или конкретная дата (15.11).",
        "kk": "Қай күнге жазайын? Мысалы: бүгін, ертең немесе нақты күн (15.11).",
    },
    "clarify_date": {
        "ru": "Уточните, пожалуйста, конкретный день: например, {opt1} или {opt2}?",
        "kk": "Нақты күнін айтыңызшы: мысалы, {opt1} әлде {opt2}?",
    },
    "clarify_weekday": {
        "ru": "Уточните, пожалуйста: сегодня ({d1}) или в следующую {wd} ({d2})?",
        "kk": "Нақтылаңызшы: бүгін ({d1}) ме, әлде келесі {wd} ({d2}) ма?",
    },
    "clarify_time": {
        "ru": "Уточните, пожалуйста, время: {h1} или {h2}?",
        "kk": "Уақытын нақтылаңызшы: {h1} әлде {h2}?",
    },
    "offer_times": {
        "ru": "На {date} есть свободное время ({service}): {times}. Какое время вам удобно?",
        "kk": "{date} күні бос уақыт бар ({service}): {times}. Қай уақыт ыңғайлы?",
    },
    "time_taken": {
        "ru": "К сожалению, {time} на {date} занято. Ближайшее свободное время: {times}. Подойдёт?",
        "kk": "Өкінішке қарай, {date} күні {time} бос емес. Жақын бос уақыт: {times}. Ыңғайлы ма?",
    },
    "no_slots": {
        "ru": "На {date} свободного времени нет. Ближайшие варианты: {options}. Подойдёт?",
        "kk": "{date} күні бос уақыт жоқ. Жақын нұсқалар: {options}. Ыңғайлы ма?",
    },
    "no_slots_at_all": {
        "ru": "К сожалению, в ближайшие дни свободного времени нет. Я передам ваш запрос администратору.",
        "kk": "Өкінішке қарай, жақын күндері бос уақыт жоқ. Сұрауыңызды әкімшіге жіберемін.",
    },
    "ask_name": {
        "ru": "{time} на {date} свободно. Подскажите, пожалуйста, как к вам обращаться?",
        "kk": "{date} күні {time} бос. Атыңызды айта аласыз ба?",
    },
    "confirm": {
        "ru": "{name}записать вас: {service}, {date} в {time}, мастер {staff}, {branch}? Ответьте «да» "
        "для подтверждения.",
        "kk": "{name}жазылуды растайсыз ба: {service}, {date}, {time}, шебер {staff}, {branch}? "
        "«Иә» деп жауап беріңіз.",
    },
    "booked": {
        "ru": "Готово! Вы записаны: {service}, {date} в {time}, мастер {staff}, {branch}, {address}. "
        "Номер брони: {number}.",
        "kk": "Жазылуыңыз расталды: {date}, {time}, {branch}, {service}, шебер {staff}. Мекенжай: "
        "{address}. Бронь №{number}.",
    },
    "booked_pending": {
        "ru": "Заявка на запись создана: {service}, {date} в {time}, мастер {staff}. Номер заявки: "
        "{number}. Администратор подтвердит её в ближайшее время.",
        "kk": "Жазылу өтінімі жасалды: {service}, {date}, {time}, шебер {staff}. Өтінім №{number}. "
        "Әкімші жақын арада растайды.",
    },
    "rescheduled": {
        "ru": "Готово! Запись перенесена: {service}, {date} в {time}, мастер {staff}. Номер брони: {number}.",
        "kk": "Жазылуыңыз ауыстырылды: {date}, {time}, {service}, шебер {staff}. Бронь №{number}.",
    },
    "declined": {
        "ru": "Хорошо. Какое время или день вам удобнее?",
        "kk": "Жарайды. Сізге қай күн немесе уақыт ыңғайлы?",
    },
    "price": {
        "ru": "«{service}» — {price} ₸, длительность {duration} мин. Хотите записаться?",
        "kk": "«{service}» — {price} ₸, ұзақтығы {duration} мин. Жазылғыңыз келе ме?",
    },
    "price_list": {
        "ru": "Наши цены: {items}. Хотите записаться?",
        "kk": "Біздің бағалар: {items}. Жазылғыңыз келе ме?",
    },
    "address": {"ru": "Наш адрес: {items}.", "kk": "Біздің мекенжай: {items}."},
    "escalated": {
        "ru": "У меня нет подтверждённой информации по этому вопросу. Я уточню у администратора и "
        "вернусь с ответом.",
        "kk": "Бұл сұрақ бойынша менде нақты ақпарат жоқ. Әкімшіден нақтылап, сізге жауап беремін.",
    },
    "no_bookings": {"ru": "У вас нет предстоящих записей.", "kk": "Сізде алдағы жазылу жоқ."},
    "cancel_confirm": {
        "ru": "Отменить запись {number}: {service}, {date} в {time}? Ответьте «да».",
        "kk": "{number} жазылуын болдырмайын ба: {service}, {date}, {time}? «Иә» деп жауап беріңіз.",
    },
    "cancelled": {
        "ru": "Запись {number} отменена. Будем рады видеть вас в другой раз!",
        "kk": "{number} жазылуы болдырылмады. Сізді басқа уақытта күтеміз!",
    },
    "cancel_kept": {"ru": "Хорошо, запись остаётся в силе.", "kk": "Жарайды, жазылу өз күшінде қалады."},
    "reschedule_ask": {
        "ru": "Запись {number}: {date} в {time}. На какой день и время перенести?",
        "kk": "{number} жазылуы: {date}, {time}. Қай күн мен уақытқа ауыстырайын?",
    },
    "thanks": {
        "ru": "Пожалуйста! Если появятся вопросы — пишите.",
        "kk": "Оқасы жоқ! Сұрақтарыңыз болса, жазыңыз.",
    },
    "error": {
        "ru": "Не получилось выполнить действие. Попробуйте выбрать другое время.",
        "kk": "Әрекет орындалмады. Басқа уақытты таңдап көріңіз.",
    },
}


def _fmt_date(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%d.%m.%Y}"


def _has(text: str, kws: tuple[str, ...]) -> bool:
    return any(k in text for k in kws)


class MockLLM:
    provider = "mock"
    model = "mock-rules-v1"

    def run(self, req: LLMRequest, execute: ToolExecutor) -> LLMResult:
        text = _Turn(req, execute).respond()
        return LLMResult(text=text, stop_reason="end_turn", iterations=1)


class _Turn:
    def __init__(self, req: LLMRequest, execute: ToolExecutor):
        self.req = req
        self.call = execute
        self.lang = req.language if req.language in ("ru", "kk") else "ru"
        self.st = req.state
        self.raw = req.latest_text or ""
        self.low = self.raw.lower().replace("ё", "е")
        self.words = set(tokens(self.raw))
        self.today = req.today or date.today()
        self._services: list[dict[str, Any]] | None = None

    # -- helpers
    def t(self, key: str, **kw: Any) -> str:
        return T[key][self.lang].format(**kw)

    def services(self) -> list[dict[str, Any]]:
        if self._services is None:
            res = self.call("list_services", {})
            self._services = res.get("services", []) if res.get("ok") else []
        return self._services

    def match_service(self) -> dict[str, Any] | None:
        q = content_tokens(self.raw)
        best, best_score = None, 0
        for s in self.services():
            score = overlap(q, content_tokens(s["title"]))
            if score > best_score:
                best, best_score = s, score
        return best

    def is_yes(self) -> bool:
        return bool(self.words & YES) and not self.words & NO

    def is_no(self) -> bool:
        return bool(self.words & NO)

    # -- entity extraction
    def parse_date(self) -> tuple[date | None, str | None]:
        """Returns (date, None) | (None, "ambiguous"/"weekday:<n>") | (None, None)."""
        low = self.low
        if _has(low, AMBIGUOUS_DATE):
            return None, "ambiguous"
        if "послезавтра" in low or "бүрсігүні" in low:
            return self.today + timedelta(days=2), None
        if "завтра" in low or "ертең" in low:
            return self.today + timedelta(days=1), None
        if "сегодня" in low or "бүгін" in low:
            return self.today, None
        if m := _DMY_RE.search(low):
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else self.today.year
            year += 2000 if year < 100 else 0
            try:
                d = date(year, month, day)
            except ValueError:
                return None, "ambiguous"
            if d < self.today and not m.group(3):
                d = date(year + 1, month, day)
            return d, None
        for m in _DAY_MONTH_RE.finditer(low):
            word = m.group(2)
            for idx, (ru, kk) in enumerate(zip(MONTHS_RU, MONTHS_KK, strict=True)):
                if (word.startswith(ru) and (ru != "ма" or word in ("мая", "май"))) or word.startswith(kk):
                    try:
                        d = date(self.today.year, idx + 1, int(m.group(1)))
                    except ValueError:
                        return None, "ambiguous"
                    return (d if d >= self.today else d.replace(year=d.year + 1)), None
        for idx, (ru, kk) in enumerate(WEEKDAYS):
            if any(w.startswith((ru, kk)) for w in self.words):
                delta = (idx - self.today.weekday()) % 7
                if delta == 0:
                    return None, f"weekday:{idx}"  # "в пятницу" said on a Friday: today or next week?
                return self.today + timedelta(days=delta), None
        return None, None

    def parse_time(self) -> tuple[str | None, bool]:
        """Returns ("HH:MM", ambiguous)."""
        if m := _TIME_RE.search(self.low):
            h, mi = int(m.group(1)), int(m.group(2))
            return f"{h:02d}:{mi:02d}", 0 < h < 8
        if m := _HOUR_RE.search(self.low):
            h = int(m.group(1))
            if 0 < h <= 23:
                return f"{h:02d}:00", 0 < h < 8
        return None, False

    def extract_name(self) -> str | None:
        m = re.search(
            r"(?:зовут|атым|есімім|я\s+—|я\s+-)\s+([^\W\d_][^\W\d_\-]{1,40})", self.raw, re.IGNORECASE
        )
        if m:
            return m.group(1).strip().capitalize()
        words = re.findall(r"[^\W\d_]+", self.raw)
        if 1 <= len(words) <= 2 and not ({w.lower() for w in words} & (YES | NO)):
            return " ".join(w.capitalize() for w in words)
        return None

    # -- main
    def respond(self) -> str:
        st, low = self.st, self.low
        awaiting = st.get("awaiting")

        if awaiting == "name" and (name := self.extract_name()):
            self.call("set_contact_name", {"name": name})
            self.req.contact_name = name
            st["awaiting"] = "confirm"
            return self.confirm_text()
        if awaiting == "confirm" and st.get("slot"):
            if self.is_yes() and not self.has_new_datetime():
                return self.finish_booking()
            if self.is_no() and not self.has_new_datetime():
                self.clear_booking()
                st["intent"] = "book"
                return self.t("declined")
        if awaiting == "cancel_confirm" and st.get("cancel_number"):
            if self.is_yes():
                return self.do_cancel()
            if self.is_no():
                st.pop("awaiting", None)
                st.pop("cancel_number", None)
                return self.t("cancel_kept")

        if _has(low, KW_CANCEL):
            return self.start_cancel()
        if _has(low, KW_RESCHEDULE):
            return self.start_reschedule()
        if _has(low, KW_DISCOUNT):
            return self.answer_from_knowledge_or_escalate()
        if _has(low, KW_PRICE) and not _has(low, KW_BOOK):
            return self.answer_price()
        if _has(low, KW_ADDRESS):
            items = "; ".join(f"{b['name']} — {b['address']}" for b in self.req.branches if b.get("address"))
            return self.t("address", items=items) if items else self.answer_from_knowledge_or_escalate()
        if (
            _has(low, KW_BOOK)
            or st.get("intent") in ("book", "reschedule")
            or self.has_new_datetime()
            or (awaiting in ("service", "date", "time"))
        ):
            return self.book_flow()
        if _has(low, KW_THANKS) and len(self.words) <= 6:
            return self.t("thanks")
        if _has(low, KW_GREETING) and len(self.words) <= 4:
            return self.t("greeting", tenant=f"«{self.req.tenant_name}»" if self.req.tenant_name else "")
        return self.answer_from_knowledge_or_escalate()

    def has_new_datetime(self) -> bool:
        d, amb = self.parse_date()
        tm, _ = self.parse_time()
        return bool(d or amb or tm)

    # -- FAQ / escalation
    def answer_from_knowledge_or_escalate(self) -> str:
        if self.req.articles and not _has(self.low, KW_DISCOUNT):
            art = self.req.articles[0]
            return art["content"].strip()[:700]
        if self.req.articles and _has(self.low, KW_DISCOUNT):
            disc = [
                a for a in self.req.articles if _has(a["content"].lower() + a["title"].lower(), KW_DISCOUNT)
            ]
            if disc:
                return disc[0]["content"].strip()[:700]
        self.call("escalate_to_admin", {"question": self.raw[:1500], "priority": "normal"})
        if _has(self.low, KW_PRICE + KW_DISCOUNT):
            self.call("add_tag", {"tag": TAG_ASKED_PRICE})
        return self.t("escalated")

    def answer_price(self) -> str:
        svc = self.match_service()
        if svc is not None:
            self.call("update_lead", {"stage": "info_requested", "service_id": svc["service_id"]})
            self.call("add_tag", {"tag": TAG_ASKED_PRICE})
            self.st.update(service_id=svc["service_id"], service_title=svc["title"])
            return self.t("price", service=svc["title"], price=svc["price"], duration=svc["duration_min"])
        q = content_tokens(self.raw)
        generic = not [w for w in q if not _has(w, (*KW_PRICE, "услуг", "прайс", "қызмет"))]
        if generic and self.services():
            self.call("update_lead", {"stage": "info_requested"})
            items = ", ".join(f"{s['title']} — {s['price']} ₸" for s in self.services())
            return self.t("price_list", items=items)
        return self.answer_from_knowledge_or_escalate()  # unknown service/price: never invent (AT-03)

    # -- booking
    def clear_booking(self) -> None:
        for k in ("slot", "awaiting", "time"):
            self.st.pop(k, None)

    def book_flow(self) -> str:
        st = self.st
        st.setdefault("intent", "book")
        svc = self.match_service()
        if svc is not None:
            st.update(service_id=svc["service_id"], service_title=svc["title"])
        d, date_issue = self.parse_date()
        tm, time_ambiguous = self.parse_time()
        if date_issue == "ambiguous":
            st["awaiting"] = "date"
            st.pop("date", None)
            return self.t(
                "clarify_date",
                opt1=_fmt_date(self.today + timedelta(days=1)),
                opt2=_fmt_date(self.today + timedelta(days=2)),
            )
        if date_issue and date_issue.startswith("weekday:"):
            st["awaiting"] = "date"
            wd = int(date_issue.split(":")[1])
            return self.t(
                "clarify_weekday",
                d1=_fmt_date(self.today),
                wd=WEEKDAY_NAMES[self.lang][wd],
                d2=_fmt_date(self.today + timedelta(days=7)),
            )
        if d is not None:
            st["date"] = d.isoformat()
            st.pop("slot", None)
        if tm is not None:
            if time_ambiguous:
                h, m = tm.split(":")
                st["awaiting"] = "time"
                return self.t("clarify_time", h1=tm, h2=f"{int(h) + 12:02d}:{m}")
            st["time"] = tm
            st.pop("slot", None)
        if not st.get("service_id"):
            st["awaiting"] = "service"
            names = ", ".join(s["title"] for s in self.services()) or "—"
            return self.t("ask_service", services=names)
        self.call("update_lead", {"stage": "qualified", "service_id": st["service_id"]})
        if not st.get("date"):
            st["awaiting"] = "date"
            return self.t("ask_date")
        return self.find_slot()

    def find_slot(self) -> str:
        st = self.st
        args: dict[str, Any] = {"service_id": st["service_id"], "date_from": st["date"]}
        if st.get("time"):
            args["preferred_time"] = st["time"]
        res = self.call("get_available_slots", args)
        if not res.get("ok"):
            st.pop("date", None)
            st["awaiting"] = "date"
            return self.t("ask_date")
        self.call("update_lead", {"stage": "booking_pending", "service_id": st["service_id"]})
        slots = res.get("slots", [])
        date_txt = _fmt_date(st["date"])
        if not slots:
            self.call("add_tag", {"tag": TAG_NO_SLOT})
            nxt = self.call(
                "get_available_slots",
                {
                    "service_id": st["service_id"],
                    "date_from": (date.fromisoformat(st["date"]) + timedelta(days=1)).isoformat(),
                    "date_to": (date.fromisoformat(st["date"]) + timedelta(days=7)).isoformat(),
                    **({"preferred_time": st["time"]} if st.get("time") else {}),
                },
            )
            alts = nxt.get("slots", [])[:3] if nxt.get("ok") else []
            if not alts:
                return self.t("no_slots_at_all")
            st["awaiting"] = "time"
            options = ", ".join(f"{_fmt_date(a['date'])} {a['time']}" for a in alts)
            return self.t("no_slots", date=date_txt, options=options)
        if st.get("time"):
            exact = next((s for s in slots if s["time"] == st["time"]), None)
            if exact is None:
                st["awaiting"] = "time"
                times = ", ".join(sorted({s["time"] for s in slots[:3]}))
                return self.t("time_taken", time=st["time"], date=date_txt, times=times)
            st["slot"] = exact
            if not self.req.contact_name:
                st["awaiting"] = "name"
                return self.t("ask_name", time=exact["time"], date=date_txt)
            st["awaiting"] = "confirm"
            return self.confirm_text()
        st["awaiting"] = "time"
        times = ", ".join(sorted({s["time"] for s in slots})[:6])
        return self.t("offer_times", date=date_txt, service=st.get("service_title", ""), times=times)

    def branch_name(self, branch_id: str | None) -> tuple[str, str]:
        for b in self.req.branches:
            if b.get("id") == branch_id:
                return b.get("name", ""), b.get("address", "")
        b0 = self.req.branches[0] if self.req.branches else {}
        return b0.get("name", ""), b0.get("address", "")

    def confirm_text(self) -> str:
        slot = self.st["slot"]
        name = self.req.contact_name
        branch, _ = self.branch_name(slot.get("branch_id"))
        prefix = f"{name}, " if name else ""
        text = self.t(
            "confirm",
            name=prefix,
            service=self.st.get("service_title", ""),
            date=_fmt_date(slot["date"]),
            time=slot["time"],
            staff=slot.get("staff_name", ""),
            branch=branch,
        )
        return text[0].upper() + text[1:]

    def finish_booking(self) -> str:
        st = self.st
        slot = st["slot"]
        if st.get("intent") == "reschedule" and st.get("reschedule_number"):
            res = self.call(
                "reschedule_booking", {"booking": st["reschedule_number"], "new_slot_id": slot["slot_id"]}
            )
            key = "rescheduled"
        else:
            res = self.call("create_booking", {"slot_id": slot["slot_id"], "service_id": st["service_id"]})
            key = "booked"
        if not res.get("ok"):
            alts = res.get("alternatives") or []
            self.clear_booking()
            if alts:
                st["awaiting"] = "time"
                times = ", ".join(f"{_fmt_date(a['date'])} {a['time']}" for a in alts[:3])
                return self.t("time_taken", time=slot["time"], date=_fmt_date(slot["date"]), times=times)
            return self.t("error")
        if key == "booked" and res.get("status") == "pending":
            key = "booked_pending"
        for k in ("slot", "awaiting", "time", "date", "intent", "reschedule_number"):
            st.pop(k, None)
        st["last_booking_number"] = res.get("booking_number")
        return self.t(
            key,
            service=res.get("service", ""),
            date=_fmt_date(res["date"]),
            time=res["time"],
            staff=res.get("staff_name", ""),
            branch=res.get("branch", ""),
            address=res.get("address", ""),
            number=res.get("booking_number", ""),
        )

    # -- cancel / reschedule
    def _my_bookings(self) -> list[dict[str, Any]]:
        res = self.call("list_my_bookings", {})
        return res.get("bookings", []) if res.get("ok") else []

    def start_cancel(self) -> str:
        bookings = self._my_bookings()
        if not bookings:
            return self.t("no_bookings")
        b = bookings[0]
        self.st.update(awaiting="cancel_confirm", cancel_number=b["booking_number"])
        return self.t(
            "cancel_confirm",
            number=b["booking_number"],
            service=b["service"],
            date=_fmt_date(b["date"]),
            time=b["time"],
        )

    def do_cancel(self) -> str:
        number = self.st.pop("cancel_number")
        self.st.pop("awaiting", None)
        res = self.call("cancel_booking", {"booking": number, "reason": "client request"})
        if not res.get("ok"):
            return self.t("error")
        return self.t("cancelled", number=number)

    def start_reschedule(self) -> str:
        bookings = self._my_bookings()
        if not bookings:
            return self.t("no_bookings")
        b = bookings[0]
        self.st.update(intent="reschedule", reschedule_number=b["booking_number"])
        if b.get("service_id"):
            self.st.update(service_id=b["service_id"], service_title=b["service"])
        if self.has_new_datetime():
            return self.book_flow()
        self.st["awaiting"] = "date"
        return self.t("reschedule_ask", number=b["booking_number"], date=_fmt_date(b["date"]), time=b["time"])
