"""Чистая логика казино-бота без Telegram: парсинг, карты, видеопокер, рулетка, слоты.

Всё здесь можно тестировать отдельно от бота.
"""
import random
import re
from collections import Counter

# Криптостойкий генератор: результаты нельзя предсказать
RNG = random.SystemRandom()

# ------------------------------------------------------------------
# Команды и суммы
# ------------------------------------------------------------------
# Принимает "/slots 100", "!профиль", "/slots@MyBot 100"
_CMD_RE = re.compile(r"^[!/]([^\s@]+)(?:@\w+)?(?:\s+(.*))?$", re.S)


def parse_command(text: str):
    """'!Профиль' -> ('профиль', ''); '/slots@bot 5к' -> ('slots', '5к'); не команда -> None."""
    mt = _CMD_RE.match(text.strip())
    if not mt:
        return None
    return mt.group(1).lower(), (mt.group(2) or "").strip()


ALL_WORDS = {"all", "allin", "все", "всё", "олл"}
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)(кк|kk|к|k|м|m)?")
_SUFFIX = {"к": 1_000, "k": 1_000, "кк": 1_000_000, "kk": 1_000_000, "м": 1_000_000, "m": 1_000_000}


def is_all(raw: str) -> bool:
    return raw.strip().lower() in ALL_WORDS


def parse_amount(raw: str) -> int | None:
    """'500' -> 500, '10к' -> 10000, '1.5к' -> 1500, '2кк' -> 2000000. Иначе None."""
    mt = _AMOUNT_RE.fullmatch(raw.strip().lower().replace("_", ""))
    if not mt:
        return None
    number, suffix = mt.group(1), mt.group(2)
    value = float(number.replace(",", "."))
    if suffix:
        value *= _SUFFIX[suffix]
    elif "." in number or "," in number:
        return None  # дробные монеты без к/м не бывают
    if abs(value - round(value)) > 1e-6:
        return None
    return int(round(value))


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


# ------------------------------------------------------------------
# Слоты (значение 🎰 от Telegram: 1..64)
# ------------------------------------------------------------------
def slot_result(value: int) -> tuple[int, str]:
    """0=BAR, 1=виноград, 2=лимон, 3=семёрка. Возвращает (множитель, текст)."""
    v = value - 1
    left, center, right = v % 4, (v // 4) % 4, v // 16
    if left == center == right == 3:
        return 25, "🎉 ДЖЕКПОТ 777!"
    if left == center == right:
        return 8, "Три в ряд!"
    return 0, "Мимо"


# ------------------------------------------------------------------
# Рулетка (европейская, 0–36)
# ------------------------------------------------------------------
RED_NUMBERS = {1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36}
_ROULETTE_ALIASES = {
    "красное": "red", "красный": "red", "к": "red", "r": "red",
    "черное": "black", "чёрное": "black", "черный": "black", "чёрный": "black", "ч": "black", "b": "black",
    "чет": "even", "чёт": "even", "четное": "even", "чётное": "even", "e": "even",
    "нечет": "odd", "нечёт": "odd", "нечетное": "odd", "нечётное": "odd", "o": "odd",
}
ROULETTE_LABELS = {"red": "🔴 красное", "black": "⚫ чёрное", "even": "чёт", "odd": "нечет"}


def parse_roulette_choice(raw: str) -> str | None:
    c = _ROULETTE_ALIASES.get(raw.strip().lower(), raw.strip().lower())
    if c in ("red", "black", "even", "odd"):
        return c
    if c.isdigit() and 0 <= int(c) <= 36:
        return str(int(c))
    return None


def roulette_label(choice: str) -> str:
    return ROULETTE_LABELS.get(choice, f"число {choice}")


def roulette_color(n: int) -> str:
    return "🟢" if n == 0 else ("🔴" if n in RED_NUMBERS else "⚫")


def roulette_mult(choice: str, n: int) -> int:
    """Итоговый множитель ставки: 0 — проигрыш, 2 — цвет/чёт, 36 — число."""
    if choice.isdigit():
        return 36 if int(choice) == n else 0
    if n == 0:
        return 0
    if choice == "red":
        return 2 if n in RED_NUMBERS else 0
    if choice == "black":
        return 2 if n not in RED_NUMBERS else 0
    if choice == "even":
        return 2 if n % 2 == 0 else 0
    return 2 if n % 2 == 1 else 0


def spin_roulette() -> int:
    return RNG.randint(0, 36)


# ------------------------------------------------------------------
# Видеопокер (Jacks or Better 8/5, несколько рук)
# ------------------------------------------------------------------
# Карта — число 0..51: ранг = c % 13 + 2 (2..14, туз = 14), масть = c // 13
SUITS = "♠♥♦♣"
_FACES = {11: "J", 12: "Q", 13: "K", 14: "A"}

# Множители на одну руку (включая саму ставку)
PAYTABLE = {
    "royal": 800,
    "sf": 50,
    "quads": 25,
    "fh": 8,
    "flush": 5,
    "straight": 4,
    "trips": 3,
    "two_pair": 2,
    "jacks": 1,
}
HAND_NAMES = {
    "royal": "Роял-флеш",
    "sf": "Стрит-флеш",
    "quads": "Каре",
    "fh": "Фулл-хаус",
    "flush": "Флеш",
    "straight": "Стрит",
    "trips": "Тройка",
    "two_pair": "Две пары",
    "jacks": "Пара J и выше",
    "nothing": "Мимо",
}


def card_str(c: int) -> str:
    r = c % 13 + 2
    return f"{_FACES.get(r, str(r))}{SUITS[c // 13]}"


def new_hand() -> list[int]:
    return RNG.sample(range(52), 5)


def evaluate(hand: list[int]) -> str:
    """Возвращает ключ комбинации (см. PAYTABLE) или 'nothing'."""
    ranks = [c % 13 + 2 for c in hand]
    counts = Counter(ranks)
    flush = len({c // 13 for c in hand}) == 1
    uniq = sorted(counts)
    straight = len(uniq) == 5 and (uniq[-1] - uniq[0] == 4 or uniq == [2, 3, 4, 5, 14])
    if straight and flush:
        return "royal" if uniq == [10, 11, 12, 13, 14] else "sf"
    freq = sorted(counts.values(), reverse=True)
    if freq[0] == 4:
        return "quads"
    if freq == [3, 2]:
        return "fh"
    if flush:
        return "flush"
    if straight:
        return "straight"
    if freq[0] == 3:
        return "trips"
    if freq == [2, 2, 1]:
        return "two_pair"
    if freq[0] == 2:
        pair_rank = next(r for r, n in counts.items() if n == 2)
        if pair_rank >= 11:
            return "jacks"
    return "nothing"


def draw_hands(initial: list[int], held_idx: set[int], n_hands: int) -> list[list[int]]:
    """Оставленные карты копируются во все руки, остальные добираются из колоды (47 карт)."""
    held = [initial[i] for i in sorted(held_idx)]
    rest = [c for c in range(52) if c not in initial]
    need = 5 - len(held)
    return [held + RNG.sample(rest, need) for _ in range(n_hands)]
