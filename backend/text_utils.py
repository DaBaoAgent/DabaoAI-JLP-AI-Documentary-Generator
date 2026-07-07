from __future__ import annotations

import re
import unicodedata

CN_DIGITS = "零一二三四五六七八九"
CN_VALUE = {char: value for value, char in enumerate(CN_DIGITS)} | {"〇": 0, "两": 2}


def _section_to_chinese(number: int) -> str:
    if number == 0:
        return ""
    units = ["", "十", "百", "千"]
    parts, zero_pending, position = [], False, 0
    while number:
        digit = number % 10
        if digit:
            if zero_pending and parts:
                parts.append("零")
            parts.extend([units[position], CN_DIGITS[digit]])
            zero_pending = False
        elif parts:
            zero_pending = True
        number //= 10
        position += 1
    result = "".join(reversed(parts))
    return result[1:] if result.startswith("一十") else result


def integer_to_chinese(number: int) -> str:
    if number == 0:
        return "零"
    if number < 0:
        return "负" + integer_to_chinese(-number)
    groups = []
    while number:
        groups.append(number % 10000)
        number //= 10000
    large_units = ["", "万", "亿", "兆"]
    result, zero_between = "", False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if group == 0:
            if result:
                zero_between = True
            continue
        if result and (zero_between or group < 1000):
            result += "零"
        result += _section_to_chinese(group) + large_units[index]
        zero_between = False
    return result


def number_to_chinese(value: str) -> str:
    if "." in value:
        integer, decimal = value.split(".", 1)
        return integer_to_chinese(int(integer or 0)) + "点" + "".join(CN_DIGITS[int(x)] for x in decimal)
    return integer_to_chinese(int(value))


def arabic_numbers_for_speech(text: str) -> str:
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", lambda m: "百分之" + number_to_chinese(m.group(1)), text)
    text = re.sub(r"(?<!\d)(\d{3,4})(?=年)",
                  lambda m: "".join(CN_DIGITS[int(x)] for x in m.group(1)), text)
    return re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])",
                  lambda m: number_to_chinese(m.group(1)), text)


def _chinese_number_value(value: str) -> int | str:
    if "点" in value:
        integer, decimal = value.split("点", 1)
        left = _chinese_number_value(integer)
        right = "".join(str(CN_VALUE[x]) for x in decimal if x in CN_VALUE)
        return f"{left}.{right}" if right else left
    if not any(x in value for x in "十百千万亿兆"):
        return int("".join(str(CN_VALUE[x]) for x in value))
    total = section = number = 0
    small_units = {"十": 10, "百": 100, "千": 1000}
    large_units = {"万": 10_000, "亿": 100_000_000, "兆": 1_000_000_000_000}
    for char in value:
        if char in CN_VALUE:
            number = CN_VALUE[char]
        elif char in small_units:
            section += (number or 1) * small_units[char]
            number = 0
        elif char in large_units:
            section += number
            total = (total + section) * large_units[char]
            section = number = 0
    return total + section + number


CN_NUMBER_PATTERN = r"[零〇一二两三四五六七八九十百千万亿兆点]+"


DISPLAY_NUMBER_PROTECT_PATTERNS = (
    r"[零〇一二两三四五六七八九十百千万亿兆点]+分之[零〇一二两三四五六七八九十百千万亿兆点]+",
    r"一落千丈",
)


def chinese_numbers_for_display(text: str) -> str:
    text = re.sub(rf"百分之({CN_NUMBER_PATTERN})",
                  lambda m: f"{_chinese_number_value(m.group(1))}%", text)

    def replace_sequence(match: re.Match) -> str:
        value = match.group(0)
        if len(value) == 1 and value not in "十百千万亿兆":
            return value
        return str(_chinese_number_value(value))

    text = re.sub(CN_NUMBER_PATTERN, replace_sequence, text)
    measure_words = "年月日艘座门枚次名人公里海里节秒分吨英寸码架发度"
    return re.sub(rf"(?<![另每某这那])([零〇一二两三四五六七八九])(?=[{measure_words}])",
                  lambda m: str(CN_VALUE[m.group(1)]), text)


def chinese_numbers_for_subtitle_display(text: str) -> str:
    protected: dict[str, str] = {}

    def keep(match: re.Match) -> str:
        token = f"__SUBTITLE_KEEP_{len(protected)}__"
        protected[token] = match.group(0)
        return token

    for pattern in DISPLAY_NUMBER_PROTECT_PATTERNS:
        text = re.sub(pattern, keep, text)
    text = chinese_numbers_for_display(text)
    for token, value in protected.items():
        text = text.replace(token, value)
    return text


def subtitle_single_line_text(text: str, smart_display_numbers: bool = True) -> str:
    if smart_display_numbers:
        text = chinese_numbers_for_subtitle_display(text)
    boundary = set("。！？；!?;,.，")
    result = []
    for index, char in enumerate(text):
        if (char == "." and index > 0 and index + 1 < len(text)
                and text[index - 1].isdigit() and text[index + 1].isdigit()):
            result.append(char)
            continue
        if char in boundary:
            result.append(" ")
        elif char == "%" or not unicodedata.category(char).startswith("P"):
            result.append(char)
    return re.sub(r"\s+", " ", "".join(result)).strip()
