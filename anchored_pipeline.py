"""Anchored documentary narration pipeline.

The v1 pipeline guessed source positions after writing a free-form script.  This
pipeline keeps source subtitle ranges attached to every narration sentence,
synthesises one audio file per sentence, and allocates non-overlapping source
intervals globally before rendering a muted video.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
import wave

from backend.concurrency import get_concurrency
from backend.media_tools import ffmpeg, ffprobe, gpt_sovits_python

ROOT = Path(__file__).resolve().parent
MAIN_PROMPT_PATH = Path(os.environ.get(
    "DABAOAI_MAIN_PROMPT_PATH",
    str(ROOT / "prompts" / "main_prompt.txt"),
))


def force_ipv4() -> None:
    """DashScope WebSocket is unreliable when Windows selects an unreachable IPv6 route."""
    original = socket.getaddrinfo

    def getaddrinfo_v4(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = getaddrinfo_v4
    original_connect = socket.create_connection

    def connect_v4(address, timeout=None, source_address=None, **kwargs):
        host, port = address[:2]
        error = None
        for family, kind, proto, _, sockaddr in original(host, port, socket.AF_INET, socket.SOCK_STREAM):
            sock = None
            try:
                sock = socket.socket(family, kind, proto)
                if timeout is not None:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                error = exc
                if sock:
                    sock.close()
        raise error or OSError(f"IPv4 connection failed: {host}:{port}")

    socket.create_connection = connect_v4
    os.environ.setdefault("PREFER_IPV4", "1")


def run(cmd: list[str], *, timeout: int | None = None, capture: bool = True) -> subprocess.CompletedProcess:
    if cmd:
        if cmd[0] == "ffmpeg":
            cmd = [ffmpeg(), *cmd[1:]]
        elif cmd[0] == "ffprobe":
            cmd = [ffprobe(), *cmd[1:]]
    return subprocess.run(cmd, check=True, text=True, encoding="utf-8", errors="replace",
                          capture_output=capture, timeout=timeout)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text("utf-8-sig").splitlines():
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def read_text_fallback(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding).strip()
        except UnicodeDecodeError:
            continue
        except OSError:
            return ""
    return path.read_text(errors="replace").strip()


def load_main_narration_prompt() -> str:
    return read_text_fallback(MAIN_PROMPT_PATH)


CN_DIGITS = "零一二三四五六七八九"


def _digitwise(value: str) -> str:
    return "".join(CN_DIGITS[int(ch)] for ch in value if ch.isdigit())


def _section_to_cn(number: int) -> str:
    units = ["", "十", "百", "千"]
    parts: list[str] = []
    zero_pending = False
    for position in range(3, -1, -1):
        divisor = 10 ** position
        digit = number // divisor
        number %= divisor
        if digit:
            if zero_pending and parts:
                parts.append("零")
            parts.append(CN_DIGITS[digit] + units[position])
            zero_pending = False
        elif parts:
            zero_pending = True
    result = "".join(parts)
    return result[1:] if result.startswith("一十") else result


def _int_to_cn(number: int) -> str:
    if number == 0:
        return "零"
    if number < 0:
        return "负" + _int_to_cn(-number)
    groups = []
    while number:
        groups.append(number % 10000)
        number //= 10000
    large_units = ["", "万", "亿", "兆"]
    result = ""
    zero_between = False
    for index in range(len(groups) - 1, -1, -1):
        group = groups[index]
        if not group:
            if result:
                zero_between = True
            continue
        if result and (zero_between or group < 1000):
            result += "零"
        result += _section_to_cn(group) + large_units[index]
        zero_between = False
    return result


def _number_to_cn(value: str) -> str:
    value = value.strip()
    if value.startswith("+"):
        value = value[1:]
    if value.startswith("-"):
        return "负" + _number_to_cn(value[1:])
    if "." in value:
        integer, decimal = value.split(".", 1)
        return _int_to_cn(int(integer or 0)) + "点" + "".join(CN_DIGITS[int(ch)] for ch in decimal if ch.isdigit())
    return _int_to_cn(int(value or 0))


def _clock_tail_to_cn(value: str) -> str:
    number = int(value)
    if number == 0:
        return "零"
    if number < 10 and len(value) > 1:
        return "零" + CN_DIGITS[number]
    return _int_to_cn(number)


def _time_match_to_cn(match: re.Match) -> str:
    result = f"{_int_to_cn(int(match.group(1)))}点{_clock_tail_to_cn(match.group(2))}分"
    if match.group(3):
        result += f"{_clock_tail_to_cn(match.group(3))}秒"
    return result


def _normalize_tts_speech_text(text: str) -> str:
    """Build temporary TTS reading text without changing subtitles."""
    text = text.strip()
    text = re.sub(r"(?<=[\u4e00-\u9fff])[\.\u00b7·](?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)",
                  lambda m: f"{_digitwise(m.group(1))}年{_int_to_cn(int(m.group(2)))}月{_int_to_cn(int(m.group(3)))}日",
                  text)
    text = re.sub(r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日",
                  lambda m: f"{_digitwise(m.group(1))}年{_int_to_cn(int(m.group(2)))}月{_int_to_cn(int(m.group(3)))}日",
                  text)
    text = re.sub(r"(?<!\d)(\d{3,4})(?=年)",
                  lambda m: _digitwise(m.group(1)), text)
    text = re.sub(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)", _time_match_to_cn, text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*%", lambda m: "百分之" + _number_to_cn(m.group(1)), text)
    text = re.sub(r"\bQ([1-4])\b", lambda m: f"第{_int_to_cn(int(m.group(1)))}季度", text, flags=re.I)
    text = re.sub(r"(?<![A-Za-z0-9])([A-Za-z]{1,8})-(\d+(?:\.\d+)?)(?![A-Za-z0-9])",
                  lambda m: f"{m.group(1)} {_number_to_cn(m.group(2))}", text)
    text = re.sub(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*[-~～]\s*(\d+(?:\.\d+)?)(?![A-Za-z])",
                  lambda m: f"{_number_to_cn(m.group(1))}到{_number_to_cn(m.group(2))}", text)
    text = re.sub(r"(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)",
                  lambda m: f"{_int_to_cn(int(m.group(2)))}分之{_int_to_cn(int(m.group(1)))}", text)
    text = re.sub(r"(?<!\d)(\d{1,2})\s*:\s*(\d{1,2})(?!\d)",
                  lambda m: f"{_int_to_cn(int(m.group(1)))}比{_int_to_cn(int(m.group(2)))}", text)
    text = re.sub(r"(?<!\d)(\d{3,4})\s*[pP]\b", lambda m: _digitwise(m.group(1)) + "P", text)
    text = re.sub(r"(?<!\d)(\d+(?:\.\d+)?)\s*[kK]\b", lambda m: _number_to_cn(m.group(1)) + "K", text)
    text = re.sub(r"(?<!\d)(\d+(?:\.\d+)?)\s*fps\b",
                  lambda m: "每秒" + _number_to_cn(m.group(1)) + "帧", text, flags=re.I)
    unit_map = {
        "kg": "千克", "km": "公里", "m": "米", "cm": "厘米", "mm": "毫米",
        "s": "秒", "ms": "毫秒", "h": "小时",
    }
    for unit, spoken in sorted(unit_map.items(), key=lambda item: -len(item[0])):
        text = re.sub(rf"(?<!\d)(\d+(?:\.\d+)?)\s*{unit}\b",
                      lambda m, spoken=spoken: _number_to_cn(m.group(1)) + spoken,
                      text, flags=re.I)
    text = re.sub(r"(?<!\d)0\d+(?!\d)", lambda m: _digitwise(m.group(0)), text)
    text = re.sub(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])",
                  lambda m: _number_to_cn(m.group(1)), text)
    return text


def prepare_tts_speech_script(segments: list["NarrationSegment"], folder: Path) -> dict[int, str]:
    speech_texts = {segment.segment_id: _normalize_tts_speech_text(segment.text)
                    for segment in segments}
    output = folder / "\u914d\u97f3\u7a3f_\u6717\u8bfb\u7248.txt"
    output.write_text("\n".join(speech_texts[segment.segment_id] for segment in segments), "utf-8")
    return speech_texts


def probe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)])
    return float(p.stdout.strip())


def srt_time(value: str) -> float:
    h, m, rest = value.replace(".", ",").split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def format_srt_time(value: float) -> str:
    value = max(0.0, value)
    ms = round(value * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@dataclass
class Subtitle:
    idx: int
    start: float
    end: float
    text: str


@dataclass
class SourceChunk:
    chunk_id: int
    start: float
    end: float
    zh: str
    en: str


@dataclass
class NarrationSegment:
    segment_id: int
    text: str
    source_chunk_ids: list[int]
    source_start: float
    source_end: float
    visual_intent: str
    importance: str
    audio_file: str = ""
    audio_duration: float = 0.0
    output_start: float = 0.0
    output_end: float = 0.0
    clip_start: float = 0.0
    clip_end: float = 0.0
    match_confidence: str = ""


def parse_srt(path: Path) -> list[Subtitle]:
    text = path.read_text("utf-8-sig", errors="replace").replace("\r\n", "\n")
    entries: list[Subtitle] = []
    pattern = re.compile(
        r"(?:^|\n)(\d+)\s*\n"
        r"(\d{2}:\d{2}:\d{2}[,.]\d+)\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d+)[^\n]*\n"
        r"(.*?)(?=\n\s*\n|\Z)", re.S)
    for match in pattern.finditer(text):
        body = re.sub(r"<[^>]+>", "", match.group(4))
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            entries.append(Subtitle(int(match.group(1)), srt_time(match.group(2)),
                                    srt_time(match.group(3)), body))
    if not entries:
        raise RuntimeError(f"无法解析字幕: {path}")
    return entries


def compact_overlapping_text(entries: Iterable[Subtitle]) -> str:
    """Remove the repeated rolling-caption prefix used by YouTube subtitles."""
    result = ""
    last = ""
    for entry in entries:
        current = entry.text.strip()
        if not current or current == last:
            continue
        # English rolling captions generally repeat a suffix of the previous cue.
        max_overlap = 0
        ceiling = min(len(last), len(current))
        for size in range(1, ceiling + 1):
            if last[-size:].lower() == current[:size].lower():
                max_overlap = size
        addition = current[max_overlap:].strip()
        if addition and addition not in result[-max(80, len(addition) * 2):]:
            if result and re.match(r"[A-Za-z0-9]", addition):
                result += " "
            result += addition
        last = current
    return re.sub(r"\s+", " ", result).strip()


def make_source_chunks(zh: list[Subtitle], en: list[Subtitle], duration: float,
                       chunk_seconds: float = 18.0) -> list[SourceChunk]:
    chunks: list[SourceChunk] = []
    start = 0.0
    cid = 1
    while start < duration:
        end = min(duration, start + chunk_seconds)
        zh_items = [x for x in zh if x.end > start and x.start < end]
        en_items = [x for x in en if x.end > start and x.start < end]
        zh_text = compact_overlapping_text(zh_items)
        en_text = compact_overlapping_text(en_items)
        if zh_text or en_text:
            chunks.append(SourceChunk(cid, start, end, zh_text, en_text))
            cid += 1
        start = end
    return chunks


def deepseek_json(prompt: str, api_key: str, model: str, url: str,
                  max_tokens: int = 8000) -> object:
    last_error = None
    for attempt in range(1, 4):
        attempt_tokens = min(12000, round(max_tokens * (1 + 0.45 * (attempt - 1))))
        retry_note = ("\n上一次返回的JSON不完整。请减少冗余字段，确保JSON完整闭合。"
                      if attempt > 1 else "")
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt + retry_note}],
            "temperature": 0.25 if attempt > 1 else 0.35,
            "max_tokens": attempt_tokens,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"
        })
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                content = json.loads(response.read())["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                left = content.find("{")
                right = content.rfind("}")
                if left >= 0 and right > left:
                    return json.loads(content[left:right + 1])
                raise
        except (json.JSONDecodeError, KeyError, urllib.error.URLError) as exc:
            last_error = exc
            print(f"  [WARN] 文案JSON解析失败，第{attempt}/3次，准备重试")
    raise RuntimeError(f"文案接口连续返回无效JSON：{last_error}")


def split_source_chunks(chunks: list[SourceChunk], parts: int) -> list[list[SourceChunk]]:
    groups = []
    for index in range(parts):
        left = round(len(chunks) * index / parts)
        right = round(len(chunks) * (index + 1) / parts)
        if chunks[left:right]:
            groups.append(chunks[left:right])
    return groups


def select_source_chunks_for_narration(chunks: list[SourceChunk], target_seconds: float) -> list[SourceChunk]:
    if not chunks:
        return chunks
    max_chunks = max(36, min(160, round(target_seconds / 4)))
    if len(chunks) <= max_chunks:
        return chunks
    if max_chunks <= 1:
        indices = [0]
    else:
        indices = sorted({round(i * (len(chunks) - 1) / (max_chunks - 1)) for i in range(max_chunks)})
    return [chunks[index] for index in indices]


def trim_segments_to_char_budget(segments: list[dict], max_chars: int, min_segments: int) -> list[dict]:
    total_chars = sum(len(str(item.get("text", ""))) for item in segments)
    if total_chars <= max_chars or not segments:
        return segments

    avg_chars = max(1, total_chars / len(segments))
    target_count = max(min_segments, min(len(segments), int(max_chars / avg_chars)))
    best: list[dict] = []

    for count in range(target_count, min_segments - 1, -1):
        if count <= 1:
            indices = [0]
        else:
            indices = sorted({round(i * (len(segments) - 1) / (count - 1)) for i in range(count)})
        subset = [segments[index] for index in indices]
        if sum(len(str(item.get("text", ""))) for item in subset) <= max_chars:
            best = subset
            break

    if not best:
        best = []
        used_chars = 0
        for item in segments:
            text_len = len(str(item.get("text", "")))
            if best and used_chars + text_len > max_chars:
                continue
            best.append(item)
            used_chars += text_len
            if used_chars >= max_chars:
                break

    for index, item in enumerate(best, 1):
        item["segment_id"] = index
    return best


def narration_prompt(chunks: list[SourceChunk], target_seconds: float, style: str = "",
                     custom_prompt: str = "", chars_per_second: float = 4.1,
                     part_index: int = 1, part_count: int = 1) -> str:
    target_chars = round(target_seconds * chars_per_second)
    minimum = round(target_chars * 0.94)
    maximum = round(target_chars * 1.06)
    source = "\n".join(
        f"[{c.chunk_id}] {format_srt_time(c.start)}-{format_srt_time(c.end)}\n"
        f"中文：{c.zh}\n英文：{c.en}" for c in chunks)
    if part_count == 1:
        structure_rule = "必须有完整开头、发展和结尾；开头前三句给出核心冲突或最强事实，结尾明确交代结果与影响。"
    elif part_index == 1:
        structure_rule = f"这是全文第1/{part_count}部分：负责高留存开头并进入事件，不要提前总结或收尾。"
    elif part_index == part_count:
        structure_rule = f"这是全文第{part_index}/{part_count}部分：直接承接前文，完成高潮、结果、历史影响和明确收尾，不要重新开场。"
    else:
        structure_rule = f"这是全文第{part_index}/{part_count}部分：直接承接前一部分，持续提供信息增量，不要重新开场、不要总结全文。"
    source_text = "\n".join(
        f"[{c.chunk_id}] {format_srt_time(c.start)}-{format_srt_time(c.end)}\n"
        f"中文：{c.zh}\n英文：{c.en}" for c in chunks)
    if part_count == 1:
        clean_structure_rule = "必须有完整开头、发展和结尾；开头前三句给出核心冲突、反常识结论或最强谜题，结尾按主提示词完成收束。"
    elif part_index == 1:
        clean_structure_rule = f"这是全文第 {part_index}/{part_count} 部分：负责高留存开头并进入事件，不要提前总结或收尾。"
    elif part_index == part_count:
        clean_structure_rule = f"这是全文第 {part_index}/{part_count} 部分：直接承接前文，完成高潮、结果、历史影响和主提示词要求的结尾，不要重新开场。"
    else:
        clean_structure_rule = f"这是全文第 {part_index}/{part_count} 部分：直接承接前一部分，持续提供信息增量，不要重新开场，不要总结全文。"
    main_prompt = load_main_narration_prompt()
    if not main_prompt:
        main_prompt = "用轻松、有料、充满悬念和反转的历史解谜说书人风格写中文解说文案。"
    style_note = style.strip() or "使用主提示词中的老肉杂谈历史解谜说书人风格"
    extra_note = custom_prompt.strip() or "无"
    prompt_revision = hashlib.sha1(main_prompt.encode("utf-8")).hexdigest()[:12]
    return f"""你正在为 DabaoAI 生成来源锚定的中文长视频解说稿。主风格提示词如下：
【主提示词 revision={prompt_revision}】
{main_prompt}

工程目标：
成片目标约 {target_seconds:.1f} 秒，正文严格控制在 {minimum}-{maximum} 个中文字符之间，不计 JSON 字段。
界面风格补充：{style_note}
本期额外要求：{extra_note}
分章结构要求：{clean_structure_rule}

关键约束：
1. 严格按原片时间顺序讲述，覆盖关键事件，压缩重复内容。
2. 每个 segments 项可以是一句或一个完整复句，程序后续会安全拆句。
3. 每句必须提供直接支持它的 source_chunk_ids，不得伪造编号；没有来源支持的内容不要写。
4. visual_intent 必须描述原片中应该出现的具体人物、地点、地图、器物、动作或场景，不要写抽象情绪。
5. 连续句尽量使用相邻但不同的来源块，避免画面重复。
6. 可以使用高留存叙事、悬念和反转，但禁止虚构事实、标题党、过度煽情和凭空补细节。
7. 中段持续提供信息增量，避免重复铺垫；结尾必须自然完成主提示词要求的收束。
8. 正文文本保持适合最终字幕阅读的自然写法，不要为了 TTS 把所有阿拉伯数字提前改成中文；阿里百炼配音前会另生成朗读版副本。

只返回 JSON 对象：{{"title":"标题","segments":[
  {{"text":"口播句","source_chunk_ids":[1,2],"visual_intent":"具体画面","importance":"core|support|transition"}}
]}}

原片字幕：
{source_text}"""

    return f"""你是中文长视频解说编导。根据带时间码的原片字幕，写一版可直接配音的中文解说稿。

成片目标约 {target_seconds:.1f} 秒，正文严格控制在 {minimum}-{maximum} 个中文字符（不计 JSON 字段）。
风格：{style or '老肉杂谈历史解谜说书人风格。'}
本期额外要求：{custom_prompt or '无'}
禁止：凭空补事实、悬念党、鸡汤、时钟隐喻、闭上眼睛、首尾强行呼应、过度煽情。

关键约束：
1. 严格按原片时间顺序讲述，覆盖关键事件，压缩重复内容。
2. 输出足够覆盖目标字数的内容；每个对象可以是一个完整复句，程序会在配音前安全拆句。
3. 每句必须提供直接支持它的 source_chunk_ids，不得伪造编号。
4. visual_intent 必须描述原片中应该出现的具体人物、舰船、地图或动作，不能写抽象情绪。
5. 连续句尽量使用相邻但不同的来源块。没有来源支持的句子不要写。
6. {structure_rule}采用抖音/小红书高留存逻辑，但禁止虚假悬念、标题党和编造。
7. 中段持续提供信息增量，避免重复铺垫；结尾交代事件结果、历史影响或明确结论，不能突然结束。
8. 所有阿拉伯数字必须写成适合中文配音的中文汉字，年份逐位写，例如“一九四三年”；百分数写成“百分之三十六”。

只返回 JSON 对象：
{{"title":"标题","segments":[
  {{"text":"口播句","source_chunk_ids":[1,2],"visual_intent":"具体画面","importance":"core|support|transition"}}
]}}

原片字幕：
{source}"""


def _clip_narration_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", "", text).strip()
    if len(text) <= limit:
        return text
    floor = max(18, round(limit * 0.55))
    best = -1
    for mark in "。！？；，,.!?;":
        pos = text.rfind(mark, 0, limit + 1)
        if pos >= floor:
            best = max(best, pos + 1)
    if best > 0:
        return text[:best].strip()
    return text[:limit].strip()


def fallback_narration_part(group: list[SourceChunk], seconds: float, index: int,
                            count: int, chars_per_second: float) -> dict:
    if not group:
        return {"title": "纪录片解说", "segments": []}
    target_chars = max(180, round(seconds * chars_per_second * 0.9))
    segment_count = max(4, min(18, round(seconds / 22)))
    if segment_count > len(group):
        segment_count = len(group)
    if segment_count <= 1:
        selected = [0]
    else:
        selected = sorted({round(i * (len(group) - 1) / (segment_count - 1)) for i in range(segment_count)})
    per_segment = max(24, round(target_chars / max(1, len(selected))))
    segments = []
    for chunk_index in selected:
        chunk = group[chunk_index]
        source_text = chunk.zh or chunk.en
        source_text = re.sub(r"\s+", "", source_text).strip()
        source_text = re.sub(r"[^\w\u4e00-\u9fff，。！？；：、,.!?;: -]+", "", source_text)
        if not source_text:
            source_text = "这一段原片继续推进事件的发展。"
        text = _clip_narration_text(source_text, per_segment)
        if len(text) < 8:
            text = "原片在这里呈现了事件继续发展的关键画面。"
        segments.append({
            "text": text,
            "source_chunk_ids": [chunk.chunk_id],
            "visual_intent": source_text[:48],
            "importance": "support" if index not in (1, count) else "core",
        })
    return {"title": "纪录片解说", "segments": segments}


def generate_narration(chunks: list[SourceChunk], target_seconds: float, api: dict[str, str],
                       output: Path, style: str = "", custom_prompt: str = "",
                       chars_per_second: float = 4.1) -> dict:
    main_prompt_revision = hashlib.sha1(load_main_narration_prompt().encode("utf-8")).hexdigest()[:12]
    if output.exists():
        try:
            cached = json.loads(output.read_text("utf-8"))
            if (cached.get("script_format_version") == 7
                    and abs(float(cached.get("target_seconds", -9999)) - target_seconds) < 0.5
                    and abs(float(cached.get("chars_per_second", -9999)) - chars_per_second) < 0.05
                    and int(cached.get("source_chunk_count", -1)) == len(chunks)
                    and cached.get("style") == style
                    and cached.get("custom_prompt") == custom_prompt
                    and cached.get("main_prompt_revision") == main_prompt_revision):
                return cached
        except (json.JSONDecodeError, OSError):
            corrupt = output.with_suffix(f".corrupt-{int(time.time())}.json")
            output.replace(corrupt)
            print(f"  [WARN] 损坏的文案缓存已隔离：{corrupt.name}")

    part_count = 1 if target_seconds <= 360 else max(1, math.ceil(target_seconds / 240))
    groups = split_source_chunks(chunks, part_count)
    part_count = len(groups)
    raw_segments = []
    title = ""
    def generate_part(group: list[SourceChunk], seconds: float, index: int, count: int,
                      depth: int = 0) -> dict:
        try:
            token_budget = min(9000, max(2600, round(seconds * chars_per_second * 4.5) + 1200))
            return deepseek_json(narration_prompt(group, seconds, style, custom_prompt,
                                                 chars_per_second, index, count),
                                 api["key"], api["model"], api["url"], max_tokens=token_budget)
        except RuntimeError:
            if depth >= 2 or len(group) < 4 or seconds < 60:
                print("  文案接口仍返回无效 JSON，已使用原片字幕生成保底文案")
                return fallback_narration_part(group, seconds, index, count, chars_per_second)
            print("  文案JSON仍不完整，自动拆成更小分章重试")
            sub_parts = []
            for sub_index, sub_group in enumerate(split_source_chunks(group, 2), 1):
                sub_seconds = seconds * len(sub_group) / len(group)
                sub_parts.append(generate_part(sub_group, sub_seconds, sub_index, 2, depth + 1))
            segments = []
            sub_title = ""
            for sub_part in sub_parts:
                if isinstance(sub_part, dict):
                    sub_title = sub_title or str(sub_part.get("title", ""))
                    if isinstance(sub_part.get("segments"), list):
                        segments.extend(sub_part["segments"])
            return {"title": sub_title, "segments": segments}

    for index, group in enumerate(groups, 1):
        part_seconds = target_seconds * len(group) / len(chunks)
        print(f"  文案分章 {index}/{part_count}：目标约{part_seconds:.0f}秒")
        part = generate_part(group, part_seconds, index, part_count)
        if not isinstance(part, dict) or not isinstance(part.get("segments"), list):
            raise RuntimeError(f"文案第{index}章没有返回 segments JSON")
        title = title or str(part.get("title", ""))
        raw_segments.extend(part["segments"])
    data = {"title": title, "segments": raw_segments}
    if not isinstance(data, dict) or not isinstance(data.get("segments"), list):
        raise RuntimeError("文案接口没有返回 segments JSON")
    chunk_map = {c.chunk_id: c for c in chunks}
    cleaned = []
    previous = 0
    for raw in data["segments"]:
        ids = sorted({int(x) for x in raw.get("source_chunk_ids", []) if int(x) in chunk_map})
        if not ids:
            continue
        # Enforce chronological provenance; a model mistake is rejected, not silently sorted later.
        if ids[0] < previous:
            continue
        previous = ids[0]
        text = re.sub(r"\s+", "", str(raw.get("text", ""))).strip()
        if len(text) < 5:
            continue
        # Keep sentences long enough for efficient GPT-SoVITS batches, but short enough for
        # natural subtitle timing and visual anchoring.
        parts = [p.strip() for p in re.split(r"(?<=[。！？；])", text) if p.strip()]
        merged: list[str] = []
        buffer = ""
        for part in parts:
            if len(part) > 58:
                comma_parts = [p for p in re.split(r"(?<=，)", part) if p]
            else:
                comma_parts = [part]
            for piece in comma_parts:
                if buffer and len(buffer) + len(piece) > 58:
                    merged.append(buffer)
                    buffer = piece
                else:
                    buffer += piece
                if len(buffer) >= 36 and buffer.endswith(("。", "！", "？", "；")):
                    merged.append(buffer)
                    buffer = ""
        if buffer:
            if merged and len(buffer) < 12:
                merged[-1] += buffer
            else:
                merged.append(buffer)
        for part in merged:
            cleaned.append({
                "segment_id": len(cleaned) + 1,
                "text": part,
                "source_chunk_ids": ids,
                "source_start": min(chunk_map[x].start for x in ids),
                "source_end": max(chunk_map[x].end for x in ids),
                "visual_intent": str(raw.get("visual_intent", "")),
                "importance": str(raw.get("importance", "support")),
            })
    minimum_segments = max(8, min(42, round(target_seconds / 26)))
    if len(cleaned) < minimum_segments:
        raise RuntimeError(
            f"有效锚定句只有 {len(cleaned)} 条，低于当前目标建议下限 {minimum_segments} 条"
        )
    char_count = sum(len(x["text"]) for x in cleaned)
    minimum_chars = round(target_seconds * chars_per_second * 0.72)
    maximum_chars = round(target_seconds * chars_per_second * 1.10)
    hard_minimum_chars = max(160, round(minimum_chars * 0.45))
    if char_count < hard_minimum_chars:
        raise RuntimeError(f"文案只有 {char_count} 字，内容明显不足，最低需要 {hard_minimum_chars} 字")
    if char_count > maximum_chars:
        print(f"  文案 {char_count} 字超过硬上限 {maximum_chars}，按时间线均匀裁剪")
        cleaned = trim_segments_to_char_budget(cleaned, maximum_chars, minimum_segments)
        char_count = sum(len(x["text"]) for x in cleaned)
    if not minimum_chars <= char_count <= maximum_chars:
        print(f"  [WARN] 文案 {char_count} 字偏离建议范围 {minimum_chars}-{maximum_chars}，"
              "目标时长为软目标，继续使用真实配音时长")
    data["segments"] = cleaned
    data["target_seconds"] = target_seconds
    data["style"] = style
    data["custom_prompt"] = custom_prompt
    data["chars_per_second"] = chars_per_second
    data["source_chunk_count"] = len(chunks)
    data["main_prompt_revision"] = main_prompt_revision
    data["script_format_version"] = 7
    temp_output = output.with_suffix(".tmp")
    temp_output.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temp_output.replace(output)
    return data


def write_plain_script(data: dict, output: Path) -> None:
    output.write_text("\n".join(x["text"] for x in data["segments"]), "utf-8")


def synthesize_cosyvoice(segments: list[NarrationSegment], folder: Path,
                         api_key: str, model: str, voice: str, rate: float,
                         speech_texts: dict[int, str] | None = None) -> None:
    force_ipv4()
    try:
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
    except ImportError as exc:
        raise RuntimeError("缺少 dashscope；请先 pip install dashscope") from exc
    dashscope.api_key = api_key
    seg_dir = folder / "_anchored_tts"
    seg_dir.mkdir(exist_ok=True)
    for i, segment in enumerate(segments, 1):
        tts_text = (speech_texts or {}).get(segment.segment_id, segment.text)
        digest = hashlib.sha1(tts_text.encode("utf-8")).hexdigest()[:10]
        target = seg_dir / f"tts_{i:04d}_{digest}.mp3"
        if not target.exists() or target.stat().st_size < 1000:
            syn = SpeechSynthesizer(model=model, voice=voice,
                                    format=AudioFormat.MP3_24000HZ_MONO_256KBPS,
                                    speech_rate=rate)
            target.write_bytes(syn.call(tts_text))
        segment.audio_file = str(target)
        segment.audio_duration = probe_duration(target)
        print(f"  TTS {i}/{len(segments)} {segment.audio_duration:.2f}s")


def synthesize_gpt_sovits(segments: list[NarrationSegment], folder: Path, engine: Path,
                          reference: Path, prompt_text: str, rate: float,
                          polish: bool = False,
                          speech_texts: dict[int, str] | None = None) -> None:
    if not engine.exists():
        raise RuntimeError(f"GPT-SoVITS 引擎不存在: {engine}")
    jobs = folder / "_gpt_sovits_jobs.json"
    seg_dir = folder / "_anchored_tts"
    seg_dir.mkdir(exist_ok=True)
    items = []
    targets = []
    for i, segment in enumerate(segments, 1):
        tts_text = (speech_texts or {}).get(segment.segment_id, segment.text)
        digest = hashlib.sha1(tts_text.encode("utf-8")).hexdigest()[:10]
        filename = f"tts_{i:04d}_{digest}.wav"
        items.append({"text": tts_text, "filename": filename})
        targets.append(seg_dir / filename)
    payload = {
        "engine": str(engine), "reference": str(reference), "prompt_text": prompt_text,
        "output_dir": str(seg_dir), "items": items, "speed": rate, "device": "auto"
    }
    jobs.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    runner = ROOT / "gpt_sovits_batch.py"
    local_python = gpt_sovits_python(engine)
    if not local_python.exists():
        raise RuntimeError(f"GPT-SoVITS 独立运行环境不存在: {local_python}")
    run([str(local_python), str(runner), str(jobs)], timeout=12 * 3600, capture=False)
    for segment, target in zip(segments, targets):
        if not target.exists():
            raise RuntimeError(f"GPT-SoVITS 未生成 {target.name}")
        if polish:
            polished = target.with_stem(target.stem + "_polished")
            run(["ffmpeg", "-y", "-i", str(target),
                 "-af", "highpass=f=80,equalizer=f=3000:t=q:w=1:g=2,"
                        "compand=attacks=0.005:decays=0.05:points=-80/-80|-30/-10|0/-3:gain=2,"
                        "loudnorm=I=-19:TP=-1.5:LRA=7",
                 "-ar", "48000", "-ac", "1", str(polished)], timeout=300)
            target.unlink()
            polished.rename(target)
        segment.audio_file = str(target)
        segment.audio_duration = probe_duration(target)


def synthesize_qwen_clone(segments: list[NarrationSegment], folder: Path, api_key: str,
                          model: str, voice: str, rate: float,
                          speech_texts: dict[int, str] | None = None) -> None:
    """Generate sentence-aligned WAV files with a Bailian Qwen cloned voice (concurrent)."""
    force_ipv4()
    import dashscope
    from dashscope.audio.qwen_tts_realtime import (
        AudioFormat, QwenTtsRealtime, QwenTtsRealtimeCallback,
    )

    class Callback(QwenTtsRealtimeCallback):
        def __init__(self):
            self.done = threading.Event()
            self.audio = bytearray()
            self.error = None

        def on_event(self, response):
            kind = response.get("type")
            if kind == "response.audio.delta":
                self.audio.extend(base64.b64decode(response["delta"]))
            elif kind == "response.done":
                self.done.set()
            elif kind == "error":
                self.error = response
                self.done.set()

        def on_close(self, code, message):
            if code not in (None, 1000):
                self.error = {"code": code, "message": message}
            self.done.set()

    dashscope.api_key = api_key
    safe_voice = re.sub(r"[^A-Za-z0-9_-]", "_", voice)[-32:]
    seg_dir = folder / f"_anchored_tts_qwen_{safe_voice}_r{rate:.1f}"
    seg_dir.mkdir(exist_ok=True)
    progress_lock = threading.Lock()
    completed = 0
    total = len(segments)

    def _synth_single(index: int, segment: NarrationSegment) -> None:
        nonlocal completed
        tts_text = (speech_texts or {}).get(segment.segment_id, segment.text)
        digest = hashlib.sha1(tts_text.encode("utf-8")).hexdigest()[:10]
        target = seg_dir / f"tts_{index:04d}_{digest}.wav"
        if target.exists() and target.stat().st_size >= 1000:
            segment.audio_file = str(target)
            segment.audio_duration = probe_duration(target)
        else:
            cb = Callback()
            tts = QwenTtsRealtime(model=model, callback=cb)
            try:
                tts.connect()
                tts.update_session(
                    voice=voice,
                    response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                    sample_rate=24000,
                    speech_rate=rate,
                    pitch_rate=1.0,
                    volume=55,
                    language_type="Chinese",
                    mode="commit",
                )
                tts.append_text(tts_text)
                tts.commit()
                if not cb.done.wait(180):
                    raise TimeoutError(f"第 {index} 段配音超时")
                if cb.error:
                    raise RuntimeError(f"第 {index} 段配音失败: {cb.error}")
                if not cb.audio:
                    raise RuntimeError(f"第 {index} 段没有返回音频")
                with wave.open(str(target), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(cb.audio)
                segment.audio_file = str(target)
                segment.audio_duration = probe_duration(target)
            finally:
                try:
                    tts.finish()
                except Exception:
                    pass
        with progress_lock:
            nonlocal completed
            completed += 1
            print(f"  Qwen TTS {completed}/{total} {segment.audio_duration:.2f}s", flush=True)

    workers = min(get_concurrency(), 5)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_synth_single, i, seg) for i, seg in enumerate(segments, 1)]
        for future in as_completed(futures):
            future.result()


def concat_audio(segments: list[NarrationSegment], folder: Path) -> Path:
    concat = folder / "_anchored_audio_concat.txt"
    pieces = []
    for segment in segments:
        pieces.append(f"file '{Path(segment.audio_file).as_posix()}'\n")
    concat.write_text("".join(pieces), "utf-8")
    output = folder / "配音.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
         "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output)], timeout=1200)
    cursor = 0.0
    for segment in segments:
        segment.output_start = cursor
        cursor += segment.audio_duration
        segment.output_end = cursor
    return output


class IntervalAllocator:
    def __init__(self, duration: float, guard: float = 0.35):
        self.duration = duration
        self.guard = guard
        self.used: list[tuple[float, float, int]] = []

    def free(self, start: float, end: float) -> bool:
        return all(end + self.guard <= a or start >= b + self.guard for a, b, _ in self.used)

    def reserve(self, start: float, end: float, segment_id: int) -> None:
        if not self.free(start, end):
            raise RuntimeError(f"素材区间重复: {start:.2f}-{end:.2f}")
        self.used.append((start, end, segment_id))

    def allocate(self, segment: NarrationSegment, min_start: float = 0.0) -> tuple[float, float, str]:
        need = segment.audio_duration
        if need <= 0:
            raise RuntimeError("音频时长无效")
        # Prefer the exact evidence interval, then progressively expand within the film.
        expansions = [0, 6, 14, 30, 60]
        for expansion in expansions:
            left = max(0.0, min_start, segment.source_start - expansion)
            right = min(self.duration, segment.source_end + expansion)
            if right - left < need:
                continue
            step = max(0.5, min(1.5, need / 4))
            # Start near the source anchor and scan outwards to avoid always taking chunk heads.
            base = max(left, min(segment.source_start, right - need))
            positions = [max(base, min_start)]
            count = int(max(0, right - left - need) / step) + 1
            for n in range(1, count + 1):
                positions.append(base + n * step)
            for start in positions:
                start = max(left, min(start, right - need))
                end = start + need
                if end <= right + 1e-6 and self.free(start, end):
                    self.reserve(start, end, segment.segment_id)
                    confidence = "A" if expansion == 0 else ("B" if expansion <= 14 else "C")
                    return start, end, confidence
        raise RuntimeError(f"第 {segment.segment_id} 句找不到无重复素材，来源 "
                           f"{segment.source_start:.1f}-{segment.source_end:.1f}")


def allocate_all(segments: list[NarrationSegment], video_duration: float,
                 usable_start: float = 0.0) -> IntervalAllocator:
    allocator = IntervalAllocator(video_duration)
    # Narration is chronological, therefore source time is a hard monotonic constraint.
    # This also makes non-overlap auditable instead of relying on a post-hoc sort.
    cursor = usable_start
    for segment in segments:
        segment.clip_start, segment.clip_end, segment.match_confidence = allocator.allocate(segment, cursor)
        cursor = segment.clip_end + allocator.guard
    return allocator


def render_video(source: Path, narration: Path, segments: list[NarrationSegment], folder: Path,
                 target_seconds: float, include_source_audio: bool = False,
                 source_volume: float = 0.5) -> Path:
    clip_dir = folder / "_anchored_clips"
    if clip_dir.exists():
        shutil.rmtree(clip_dir)
    clip_dir.mkdir()

    def _cut_clip(index: int, segment: NarrationSegment) -> None:
        clip = clip_dir / f"clip_{index:04d}.mp4"
        cmd = ["ffmpeg", "-y", "-ss", f"{segment.clip_start:.3f}", "-i", str(source),
             "-t", f"{segment.audio_duration:.3f}", "-map", "0:v:0", "-an",
             "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
             str(clip)]
        if include_source_audio:
            an_index = cmd.index("-an")
            cmd[an_index:an_index + 1] = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k"]
        run(cmd, timeout=600)

    workers = get_concurrency()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_cut_clip, i, seg) for i, seg in enumerate(segments, 1)]
        for future in as_completed(futures):
            future.result()

    for i, segment in enumerate(segments, 1):
        print(f"  视频 {i}/{len(segments)} <- {segment.clip_start:.1f}-{segment.clip_end:.1f}s")
    concat = clip_dir / "concat.txt"
    concat.write_text("".join(f"file '{(clip_dir / f'clip_{i:04d}.mp4').as_posix()}'\n"
                              for i in range(1, len(segments) + 1)), "utf-8")
    silent = folder / "_anchored_silent.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
         "-c", "copy", str(silent)], timeout=1200)
    raw_output = folder / "_anchored_muxed.mp4"
    if include_source_audio:
        run(["ffmpeg", "-y", "-i", str(silent), "-i", str(narration),
             "-filter_complex", f"[0:a]volume={source_volume:.4f}[src];"
             "[1:a][src]amix=inputs=2:duration=first:dropout_transition=2,"
             "loudnorm=I=-19:TP=-1.5:LRA=7[a]", "-map", "0:v:0", "-map", "[a]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(raw_output)],
            timeout=1200)
    else:
        run(["ffmpeg", "-y", "-i", str(silent), "-i", str(narration),
             "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-af", "loudnorm=I=-19:TP=-1.5:LRA=7", "-c:a", "aac", "-b:a", "192k",
             "-shortest", str(raw_output)], timeout=1200)
    output = folder / "★ 成片.mp4"
    shutil.copy2(raw_output, output)
    return output


def write_outputs(data: dict, segments: list[NarrationSegment], allocator: IntervalAllocator,
                  folder: Path) -> None:
    manifest = {
        "title": data.get("title", ""),
        "segments": [asdict(x) for x in segments],
        "occupied_intervals": [
            {"start": a, "end": b, "segment_id": sid} for a, b, sid in sorted(allocator.used)
        ],
        "validation": {
            "interval_overlap_count": 0,
            "source_backtrack_count": sum(
                segments[i].clip_start < segments[i - 1].clip_end
                for i in range(1, len(segments))
            ),
            "all_segments_anchored": all(x.source_chunk_ids for x in segments),
            "confidence_counts": {grade: sum(x.match_confidence == grade for x in segments)
                                  for grade in "ABC"},
        },
    }
    (folder / "★ 匹配报告.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    srt_lines = []
    for i, segment in enumerate(segments, 1):
        srt_lines.extend([str(i), f"{format_srt_time(segment.output_start)} --> "
                                 f"{format_srt_time(segment.output_end)}", segment.text, ""])
    (folder / "★ 字幕.srt").write_text("\n".join(srt_lines), "utf-8")


def discover(folder: Path) -> tuple[Path, Path, Path]:
    videos = sorted([*folder.glob("*.mp4"), *folder.glob("*.mkv")], key=lambda p: p.stat().st_size,
                    reverse=True)
    all_srt = sorted(folder.glob("*.srt"))
    zh = sorted(folder.glob("*.zh-Hans.srt")) or sorted(folder.glob("*zh*.srt")) or all_srt
    en = sorted(folder.glob("*.en-orig.srt")) or sorted(folder.glob("*.en.srt")) or all_srt
    if not videos or not all_srt:
        raise RuntimeError("素材目录必须包含视频和至少一个 SRT 字幕")
    return videos[0], zh[0], en[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="来源锚定、全局去重的纪录片解说流水线")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--ratio", type=float, default=0.5)
    parser.add_argument("--target-seconds", type=float, default=None)
    parser.add_argument("--tts-backend", choices=["gpt-sovits", "cosyvoice", "qwen-clone"],
                        default="gpt-sovits")
    parser.add_argument("--gpt-sovits", type=Path, default=Path(os.environ.get("GPT_SOVITS_ENGINE", "")))
    parser.add_argument("--reference", type=Path,
                        default=Path(os.environ.get("GPT_SOVITS_REFERENCE_AUDIO", "")))
    parser.add_argument(
        "--prompt-text",
        default="我们的主角是一只象鼩。名字里有象，长得却只有巴掌大小。如果把大象比作一辆重型卡车。",
        help="必须与参考音频逐字一致；默认值对应 dabao.wav",
    )
    parser.add_argument("--qwen-voice", default="")
    parser.add_argument("--qwen-model", default="")
    parser.add_argument("--speech-rate", type=float, default=1.0)
    parser.add_argument("--trim-head", type=float, default=6.0)
    parser.add_argument("--trim-tail", type=float, default=15.0)
    parser.add_argument("--style", default="")
    parser.add_argument("--custom-prompt", default="")
    parser.add_argument("--include-source-audio", action="store_true")
    parser.add_argument("--source-volume", type=float, default=0.5)
    parser.add_argument("--script-only", action="store_true")
    parser.add_argument("--exclude-interviews", action="store_true")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--polish", action="store_true")
    args = parser.parse_args()

    folder = args.folder.resolve()
    source, zh_path, en_path = discover(folder)
    duration = probe_duration(source)
    target_seconds = args.target_seconds if args.target_seconds is not None else duration * args.ratio
    target_seconds = max(30.0, min(target_seconds, duration))
    env = {**load_env(ROOT / ".env"), **os.environ}
    config = json.loads((ROOT / "config.json").read_text("utf-8"))
    api = {
        "key": env.get("DEEPSEEK_API_KEY", ""),
        "model": config.get("deepseek", {}).get("model", "deepseek-chat"),
        "url": config.get("deepseek", {}).get("api_url", "https://api.deepseek.com/v1/chat/completions"),
    }
    if not api["key"]:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")
    print(f"原片 {duration:.1f}s，目标 {target_seconds:.1f}s ({args.ratio:.0%})")

    chunks = make_source_chunks(parse_srt(zh_path), parse_srt(en_path), duration)
    usable_end = duration - args.trim_tail
    chunks = [c for c in chunks if c.start >= args.trim_head and c.end <= usable_end]
    if not chunks:
        raise RuntimeError("跳过片头片尾后没有可用字幕区间")
    (folder / "_source_chunks.json").write_text(
        json.dumps([asdict(x) for x in chunks], ensure_ascii=False, indent=2), "utf-8")
    narration_chunks = select_source_chunks_for_narration(chunks, target_seconds)
    if len(narration_chunks) < len(chunks):
        print(f"文案素材抽样：{len(chunks)} -> {len(narration_chunks)} 块，按全片时间线均匀覆盖")
        (folder / "_narration_source_chunks.json").write_text(
            json.dumps([asdict(x) for x in narration_chunks], ensure_ascii=False, indent=2), "utf-8")
    narration_file = folder / "_narration_manifest.json"
    chars_per_second = ((5.0 if args.tts_backend == "qwen-clone" else 4.1)
                        * args.speech_rate)
    data = generate_narration(narration_chunks, target_seconds, api, narration_file,
                              args.style, args.custom_prompt, chars_per_second)
    write_plain_script(data, folder / "配音稿.txt")
    print(f"文案 {len(data['segments'])} 句，{sum(len(x['text']) for x in data['segments'])} 字")
    if args.script_only:
        return

    segments = [NarrationSegment(**x) for x in data["segments"]]
    speech_texts = prepare_tts_speech_script(segments, folder)
    if args.tts_backend == "gpt-sovits":
        synthesize_gpt_sovits(segments, folder, args.gpt_sovits, args.reference,
                              args.prompt_text, args.speech_rate, polish=args.polish,
                              speech_texts=speech_texts)
    elif args.tts_backend == "cosyvoice":
        cosy = config.get("cosyvoice", {})
        synthesize_cosyvoice(segments, folder, env.get("DASHSCOPE_API_KEY", ""),
                             cosy.get("model", "cosyvoice-v3.5-plus"), cosy.get("voice_id", ""),
                             args.speech_rate, speech_texts=speech_texts)
    else:
        profile_path = ROOT / "voice_dabao_bailian.json"
        profile = json.loads(profile_path.read_text("utf-8")) if profile_path.exists() else {}
        voice = args.qwen_voice or profile.get("voice", "")
        model = args.qwen_model or profile.get("target_model", "qwen3-tts-vc-realtime-2026-01-15")
        if not voice:
            raise RuntimeError("未配置 Qwen 复刻音色 ID")
        synthesize_qwen_clone(segments, folder, env.get("DASHSCOPE_API_KEY", ""),
                              model, voice, args.speech_rate, speech_texts=speech_texts)
    narration = concat_audio(segments, folder)
    allocator = allocate_all(segments, usable_end, args.trim_head)

    if args.exclude_interviews:
        si_key = env.get("SILICONFLOW_API_KEY", "")
        if si_key:
            print("开始采访场景检测...")
            interview_count = 0
            for i, segment in enumerate(segments, 1):
                mid_time = (segment.clip_start + segment.clip_end) / 2
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                run(["ffmpeg", "-y", "-ss", f"{mid_time:.3f}", "-i", str(source),
                     "-vframes", "1", "-q:v", "2", str(tmp_path)], timeout=60)
                try:
                    with open(tmp_path, "rb") as f:
                        frame_b64 = base64.b64encode(f.read()).decode("ascii")
                    payload = json.dumps({
                        "model": "Qwen/Qwen2.5-VL-72B-Instruct",
                        "messages": [{"role": "user", "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                            {"type": "text",
                             "text": "这张图片是否来自一个采访（访谈、对话、人物面对面说话）场景？请只回答'是'或'否'。"}
                        ]}],
                        "temperature": 0.1,
                        "max_tokens": 8,
                    }, ensure_ascii=False).encode("utf-8")
                    req = urllib.request.Request(
                        "https://api.siliconflow.cn/v1/chat/completions",
                        data=payload,
                        headers={"Authorization": f"Bearer {si_key}",
                                 "Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=60) as resp:
                        result = json.loads(resp.read())
                    answer = result["choices"][0]["message"]["content"].strip()
                    is_interview = "是" in answer
                    if is_interview:
                        interview_count += 1
                        old_clip = (segment.clip_start, segment.clip_end)
                        need = segment.audio_duration
                        min_start = segment.clip_end + allocator.guard if i > 1 else 0.0
                        left = max(0.0, min_start, segment.source_start - 30)
                        right = min(usable_end, segment.source_end + 30)
                        best_start = left
                        best_end = left + need
                        step = max(0.5, min(1.5, need / 4))
                        base = max(left, min(segment.source_start, right - need))
                        for start_val in [base] + [base + n * step for n in range(1, int((right - left - need) / step) + 2)]:
                            start_val = max(left, min(start_val, right - need))
                            end_val = start_val + need
                            if end_val <= right + 1e-6:
                                # Check against allocator's used intervals but exclude this segment's old one
                                allocator.used = [(a, b, sid) for a, b, sid in allocator.used
                                                  if sid != segment.segment_id]
                                if allocator.free(start_val, end_val):
                                    best_start = start_val
                                    best_end = end_val
                                    break
                        allocator.reserve(best_start, best_end, segment.segment_id)
                        segment.clip_start = best_start
                        segment.clip_end = best_end
                        segment.match_confidence = "I"
                        print(f"  🔴 采访 {i}/{len(segments)}: "
                              f"{old_clip[0]:.1f}-{old_clip[1]:.1f} → "
                              f"{best_start:.1f}-{best_end:.1f}")
                finally:
                    tmp_path.unlink(missing_ok=True)
            print(f"采访检测完成: {interview_count}/{len(segments)} 段为采访场景")
        else:
            print("跳过采访检测（SiliconFlow API Key 未配置）")

    output = render_video(source, narration, segments, folder, target_seconds,
                          args.include_source_audio, args.source_volume)
    cursor = 0.0
    for segment in segments:
        segment.output_start = cursor
        cursor += segment.audio_duration
        segment.output_end = cursor
    write_outputs(data, segments, allocator, folder)
    print(f"完成：{output} ({probe_duration(output):.1f}s)")


if __name__ == "__main__":
    main()
