from __future__ import annotations

import json
import base64
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from .schemas import AppSettings
from .text_utils import DISPLAY_NUMBER_PROTECT_PATTERNS, subtitle_single_line_text
from .media_tools import ffmpeg, ffprobe


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
SUBTITLE_SPLIT_PROTECT_PATTERNS = (
    *DISPLAY_NUMBER_PROTECT_PATTERNS,
    r"\d+年代[初中末]?",
)


RESOLUTIONS = {
    "720P": (1280, 720), "1080P": (1920, 1080), "2K": (2560, 1440), "4K": (3840, 2160),
}
COVER_SIZES = {
    ("720P", "3:4"): (720, 960), ("1080P", "3:4"): (1080, 1440),
    ("720P", "9:16"): (720, 1280), ("1080P", "9:16"): (1080, 1920),
    ("720P", "4:3"): (960, 720), ("1080P", "4:3"): (1440, 1080),
    ("720P", "16:9"): (1280, 720), ("1080P", "16:9"): (1920, 1080),
}
FONT_FILES = {
    "Arial": Path(r"C:\Windows\Fonts\arial.ttf"),
    "Calibri": Path(r"C:\Windows\Fonts\calibri.ttf"),
    "Verdana": Path(r"C:\Windows\Fonts\verdana.ttf"),
    "Comic Sans MS": Path(r"C:\Windows\Fonts\comic.ttf"),
    "Impact": Path(r"C:\Windows\Fonts\impact.ttf"),
    "Ma Shan Zheng": ROOT / "frontend" / "public" / "fonts" / "MaShanZheng-Regular.ttf",
    "ZCOOL KuaiLe": ROOT / "frontend" / "public" / "fonts" / "ZCOOLKuaiLe-Regular.ttf",
    "ZCOOL XiaoWei": ROOT / "frontend" / "public" / "fonts" / "ZCOOLXiaoWei-Regular.ttf",
    "Long Cang": ROOT / "frontend" / "public" / "fonts" / "LongCang-Regular.ttf",
    "Liu Jian Mao Cao": ROOT / "frontend" / "public" / "fonts" / "LiuJianMaoCao-Regular.ttf",
    "Microsoft YaHei": Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    "SimHei": Path(r"C:\Windows\Fonts\simhei.ttf"),
    "SimSun": Path(r"C:\Windows\Fonts\simsun.ttc"),
    "KaiTi": Path(r"C:\Windows\Fonts\simkai.ttf"),
    "KaiTi Poster": Path(r"C:\Windows\Fonts\simkai.ttf"),
    "FangSong": Path(r"C:\Windows\Fonts\simfang.ttf"),
    "DengXian": Path(r"C:\Windows\Fonts\Deng.ttf"),
    "STKaiti": Path(r"C:\Windows\Fonts\simkai.ttf"),
}


def run(cmd: list[str], timeout: int = 1800) -> None:
    if cmd:
        if cmd[0] == "ffmpeg":
            cmd = [ffmpeg(), *cmd[1:]]
        elif cmd[0] == "ffprobe":
            cmd = [ffprobe(), *cmd[1:]]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-2000:])


def duration(path: Path) -> float:
    proc = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True,
                          check=True)
    return float(proc.stdout.strip())


def ass_color(hex_color: str) -> str:
    value = hex_color.lstrip("#")
    if len(value) != 6:
        value = "FFFFFF"
    return f"&H00{value[4:6]}{value[2:4]}{value[0:2]}"


def ffmpeg_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _timestamp_seconds(value: str) -> float:
    h, m, rest = value.replace(".", ",").split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _format_timestamp(total: float) -> str:
    total = max(0, total)
    millis = round(total * 1000)
    hh, millis = divmod(millis, 3_600_000)
    mm, millis = divmod(millis, 60_000)
    ss, millis = divmod(millis, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{millis:03d}"


def deepseek_subtitle_display_cleanup(texts: list[str], api_key: str, model: str,
                                      api_url: str = "https://api.deepseek.com/v1/chat/completions") -> list[str]:
    if not api_key or not texts:
        return texts
    cleaned = list(texts)
    for start in range(0, len(texts), 80):
        chunk = texts[start:start + 80]
        prompt = (
            "你是纪录片中文字幕显示校正助手。只校正烧录到画面上的字幕文本，不改变原意，"
            "不要扩写、不要改写事实、不要添加解释。把为了配音准确而写成中文大写的数字，"
            "在适合屏幕阅读时改回阿拉伯数字；但分数、成语、惯用语要保持中文。"
            "例如：三十年代 -> 30年代；四分之三保持四分之三；一落千丈保持一落千丈。"
            "去掉多余标点和空格。必须返回 JSON：{\"items\":[...]}，数量和输入完全一致。\n\n"
            f"字幕数组：{json.dumps(chunk, ensure_ascii=False)}"
        )
        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": min(6000, max(800, sum(len(x) for x in chunk) * 2)),
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(api_url, data=payload, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                content = json.loads(response.read())["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.I)
            result = json.loads(content)
            items = result.get("items")
            if isinstance(items, list) and len(items) == len(chunk):
                for index, item in enumerate(items):
                    value = subtitle_single_line_text(str(item), smart_display_numbers=False)
                    cleaned[start + index] = value or chunk[index]
        except Exception:
            continue
    return cleaned


def _protected_subtitle_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in SUBTITLE_SPLIT_PROTECT_PATTERNS:
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, text))
    return sorted(spans)


def _split_subtitle_line(text: str, max_chars: int) -> list[str]:
    if not max_chars or len(text) <= max_chars:
        return [text]
    spans = _protected_subtitle_spans(text)
    parts: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            for span_start, span_end in spans:
                if start < span_start < end < span_end:
                    end = span_start
                    break
                if span_start <= start < end < span_end and span_end - start <= max_chars:
                    end = span_end
                    break
        if end <= start:
            end = min(len(text), start + max_chars)
        part = text[start:end].strip()
        if part:
            parts.append(part)
        start = end
    return parts


def _append_subtitle_cues(cues: list[tuple[float, float, str]], start: float, end: float,
                          text: str, max_chars: int, single_line: bool) -> None:
    parts = _split_subtitle_line(text, max_chars)
    if not parts:
        return
    if not single_line or len(parts) == 1:
        cues.append((start, end, parts[0] if single_line else "\n".join(parts)))
        return

    duration = max(0.05, end - start)
    weights = [max(1, len(part)) for part in parts]
    total = sum(weights)
    cursor = start
    elapsed_weight = 0
    for index, part in enumerate(parts):
        elapsed_weight += weights[index]
        part_end = end if index == len(parts) - 1 else start + duration * elapsed_weight / total
        if part_end <= cursor:
            part_end = min(end, cursor + 0.05)
        cues.append((cursor, part_end, part))
        cursor = part_end


def _split_subtitle_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", "", text).strip()
    parts = re.findall(r"[^。！？!?；;]+[。！？!?；;]?", text)
    return [part.strip() for part in parts if part.strip()] or ([text] if text else [])


def _normalize_subtitle_text(text: str, smart_display_numbers: bool,
                             text_cleaner: Callable[[str], str] | None = None) -> str:
    text = subtitle_single_line_text(text, smart_display_numbers)
    if text_cleaner:
        text = text_cleaner(text)
    if re.search(r"[\u4e00-\u9fff]", text):
        text = re.sub(r"\s+", "", text)
    return text.strip()


def wrap_srt_file(source: Path, target: Path, max_chars: int, offset: float = 0.0,
                  single_line: bool = True,
                  smart_display_numbers: bool = True,
                  text_cleaner: Callable[[str], str] | None = None,
                  batch_text_cleaner: Callable[[list[str]], list[str]] | None = None) -> None:
    content = source.read_text("utf-8-sig", errors="replace").replace("\r\n", "\n")
    rows: list[tuple[float, float, str]] = []
    cues: list[tuple[float, float, str]] = []

    pattern = re.compile(
        r"(?:^|\n)\s*(?:\d+\s*\n)?"
        r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2}[,.]\d{3})[^\n]*\n"
        r"(.*?)(?=\n\s*\n|\Z)",
        re.S,
    )
    for match in pattern.finditer(content):
        raw_text = " ".join(line.strip() for line in match.group(3).splitlines() if line.strip())
        start = _timestamp_seconds(match.group(1)) + offset
        end = _timestamp_seconds(match.group(2)) + offset
        sentence_parts = _split_subtitle_sentences(raw_text)
        weights = [max(1, len(part)) for part in sentence_parts]
        total = sum(weights)
        cursor = start
        elapsed = 0
        for index, raw_part in enumerate(sentence_parts):
            elapsed += weights[index]
            part_end = end if index == len(sentence_parts) - 1 else start + (end - start) * elapsed / total
            text = _normalize_subtitle_text(raw_part, smart_display_numbers, text_cleaner)
            if text:
                rows.append((cursor, part_end, text))
            cursor = part_end

    if batch_text_cleaner and rows:
        cleaned_texts = batch_text_cleaner([text for _, _, text in rows])
        if len(cleaned_texts) == len(rows):
            rows = [(start, end, cleaned_texts[index] or text)
                    for index, (start, end, text) in enumerate(rows)]

    for start, end, text in rows:
        if re.search(r"[\u4e00-\u9fff]", text):
            text = re.sub(r"\s+", "", text)
        _append_subtitle_cues(cues, start, end, text, max_chars, True)

    output_lines = []
    for index, (start, end, text) in enumerate(cues, 1):
        output_lines.extend([
            str(index),
            f"{_format_timestamp(start)} --> {_format_timestamp(end)}",
            text,
            ""
        ])
    target.write_text("\n".join(output_lines), "utf-8")


def acquire_bgm(settings: AppSettings, output_dir: Path) -> tuple[Path | None, dict]:
    if settings.bgm.mode == "none":
        return None, {"mode": "none"}
    if settings.bgm.mode == "local":
        path = Path(settings.bgm.local_path.strip().strip('"'))
        if not path.is_file():
            raise RuntimeError(f"指定的 BGM 不存在：{path}")
        return path, {"mode": "local", "source": str(path)}
    script_file = output_dir / "配音稿.txt"
    script = script_file.read_text("utf-8", errors="replace") if script_file.exists() else ""
    if any(word in script for word in ("战争", "战舰", "海战", "军队", "战役")):
        track_id, mood = 587, "documentary"
    elif any(word in script for word in ("帝国", "文明", "王朝", "崛起", "史诗")):
        track_id, mood = 676, "majestic"
    else:
        track_id, mood = 292, "ambient"
    cache = RUNTIME / "bgm" / f"mixkit_{mood}_{track_id}.mp3"
    cache.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://assets.mixkit.co/music/{track_id}/{track_id}.mp3"
    if not cache.exists() or cache.stat().st_size < 10000:
        request = urllib.request.Request(url, headers={"User-Agent": "DabaoAI-JLP/2.0"})
        with urllib.request.urlopen(request, timeout=120) as response:
            cache.write_bytes(response.read())
    metadata = {
        "mode": "auto", "track": f"Mixkit {mood} #{track_id}", "source_url": url,
        "matched_mood": mood,
        "license_page": "https://mixkit.co/free-stock-music/", "notice": "发布前请复核当前授权条款",
    }
    (output_dir / "★ BGM授权信息.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), "utf-8")
    return cache, metadata


def render_final(settings: AppSettings, folder: Path, work_dir: Path, subtitle_ai_key: str = "") -> Path:
    source = folder / "★ 成片.mp4"
    subtitle = folder / "★ 字幕.srt"
    if not source.exists():
        raise RuntimeError("缺少核心成片")
    width, height = RESOLUTIONS[settings.video.resolution]
    video_filters = [f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2", "setsar=1"]
    head_buffer = settings.video.padding_head
    tail_buffer = settings.video.padding_tail
    if head_buffer or tail_buffer:
        video_filters.append(
            f"tpad=start_duration={head_buffer:.3f}:stop_duration={tail_buffer:.3f}:"
            "start_mode=clone:stop_mode=clone"
        )
    if settings.subtitle.enabled and subtitle.exists():
        local_srt = work_dir / "subtitle.srt"
        style = settings.subtitle
        batch_cleaner = None
        if style.ai_text_cleanup and subtitle_ai_key:
            batch_cleaner = lambda texts: deepseek_subtitle_display_cleanup(
                texts, subtitle_ai_key, settings.api.deepseek_model
            )
        wrap_srt_file(subtitle, local_srt, style.max_chars_per_line,
                      head_buffer + style.time_offset, style.single_line,
                      style.ai_text_cleanup, batch_text_cleaner=batch_cleaner)
        ass_font_size = max(12, round(style.size * height / 1080 * 0.55))
        ass_margin = max(4, round(style.bottom_margin * 288 / 1080))
        ass_outline = max(0, round(style.border_width * height / 1080 * 0.7))
        force_style = (
            f"FontName={style.font},FontSize={ass_font_size},PrimaryColour={ass_color(style.color)},"
            f"OutlineColour={ass_color(style.border_color)},Outline={ass_outline},"
            f"Shadow={style.shadow},MarginV={ass_margin},Alignment=2"
        )
        fonts_dir = ROOT / "frontend" / "public" / "fonts"
        subtitle_filter = f"subtitles='{ffmpeg_filter_path(local_srt)}':force_style='{force_style}'"
        if fonts_dir.exists():
            subtitle_filter += f":fontsdir='{ffmpeg_filter_path(fonts_dir)}'"
        video_filters.append(subtitle_filter)
    bgm, _ = acquire_bgm(settings, folder)
    temp = work_dir / "published.mp4"
    cmd = ["ffmpeg", "-y", "-i", str(source)]
    if bgm:
        cmd += ["-stream_loop", "-1", "-i", str(bgm)]
    cmd += ["-filter_complex"]
    if bgm:
        filters = (
            f"[0:v]{','.join(video_filters)}[v];"
            f"[0:a]volume=1.0[voice];[1:a]volume={settings.bgm.volume:.4f}[music];"
            f"[voice][music]amix=inputs=2:duration=first:dropout_transition=3,"
            f"loudnorm=I=-19:TP=-1.5:LRA=7,adelay={round(head_buffer*1000)}:all=1,"
            f"apad=pad_dur={tail_buffer:.3f}[a]"
        )
    else:
        filters = (f"[0:v]{','.join(video_filters)}[v];[0:a]loudnorm=I=-19:TP=-1.5:LRA=7,"
                   f"adelay={round(head_buffer*1000)}:all=1,apad=pad_dur={tail_buffer:.3f}[a]")
    output_duration = duration(source) + head_buffer + tail_buffer
    cmd += [filters, "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset",
            settings.video.preset, "-crf", str(settings.video.video_crf), "-c:a", "aac",
            "-b:a", "192k", "-ar", "48000", "-t", f"{output_duration:.3f}",
            "-movflags", "+faststart", str(temp)]
    run(cmd)
    shutil.move(str(temp), str(source))
    return source


def deepseek_publication(script: str, api_key: str, model: str, api_url: str) -> dict:
    prompt = f"""根据下面的中文纪录片解说稿生成发布信息。要求标题准确克制，标签恰好5个且精准，描述100-180字。
只返回 JSON：{{"title":"","tags":[""],"description":""}}
解说稿：{script[:12000]}"""
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.5, "max_tokens": 1000,
                          "response_format": {"type": "json_object"}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(api_url, data=payload, headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=180) as response:
        text = json.loads(response.read())["choices"][0]["message"]["content"]
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.I)
    return json.loads(text)


def create_publication(settings: AppSettings, folder: Path, api_key: str) -> dict:
    script_file = folder / "配音稿.txt"
    script = script_file.read_text("utf-8") if script_file.exists() else "纪录片"
    try:
        info = deepseek_publication(script, api_key, settings.api.deepseek_model,
                                    "https://api.deepseek.com/v1/chat/completions")
    except Exception:
        info = {"title": "纪录片解说", "tags": ["纪录片", "历史", "人文", "知识", "解说"],
                "description": script[:160]}
    tags = list(dict.fromkeys(info.get("tags", [])))[:5]
    while len(tags) < 5:
        tags.append(["纪录片", "历史", "人文", "知识", "解说"][len(tags)])
    info["tags"] = tags
    text = f"标题：{info.get('title','')}\n\n标签：{' '.join('#'+x.lstrip('#') for x in tags)}\n\n描述：{info.get('description','')}\n"
    (folder / "★ 发布信息.txt").write_text(text, "utf-8")
    (folder / "★ 发布信息.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), "utf-8")
    return info


def crop_cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_ratio = size[0] / size[1]
    source_ratio = image.width / image.height
    if source_ratio > target_ratio:
        width = round(image.height * target_ratio)
        left = (image.width - width) // 2
        image = image.crop((left, 0, left + width, image.height))
    else:
        height = round(image.width / target_ratio)
        top = (image.height - height) // 2
        image = image.crop((0, top, image.width, top + height))
    return image.resize(size, Image.Resampling.LANCZOS)


def seedream_cover(title: str, size: tuple[int, int], api_key: str, model: str) -> Image.Image | None:
    if not api_key:
        return None
    prompt = (
        f"BBC documentary cover art about {title}, cinematic historical realism, dramatic natural lighting, "
        "one clear main subject, strong composition, premium editorial photography, no text, no words, no logo"
    )
    payload = json.dumps({"model": model, "prompt": prompt, "size": f"{size[0]}x{size[1]}",
                          "response_format": "b64_json"}).encode("utf-8")
    request = urllib.request.Request("https://ark.cn-beijing.volces.com/api/v3/images/generations",
                                     data=payload, headers={"Authorization": f"Bearer {api_key}",
                                                            "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            result = json.loads(response.read())
        raw = base64.b64decode(result["data"][0]["b64_json"])
        from io import BytesIO
        return Image.open(BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def wrap_title(draw: ImageDraw.ImageDraw, title: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> list[str]:
    lines, current = [], ""
    for char in title:
        trial = current + char
        if current and draw.textbbox((0, 0), trial, font=font, stroke_width=2)[2] > max_width:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    return lines[:3]


def create_covers(settings: AppSettings, folder: Path, title: str, work_dir: Path,
                  seedream_key: str = "") -> list[Path]:
    video = folder / "★ 成片.mp4"
    frame = work_dir / "cover_frame.jpg"
    run(["ffmpeg", "-y", "-ss", f"{duration(video) * 0.35:.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(frame)], timeout=120)
    base = Image.open(frame).convert("RGB")
    outputs = []
    for ratio in settings.cover.ratios:
        size = COVER_SIZES[(settings.cover.size, ratio)]
        generated = seedream_cover(title, size, seedream_key, settings.api.cover_model)
        canvas = crop_cover(generated or base, size)
        canvas = ImageEnhance.Contrast(canvas).enhance(1.08)
        overlay = Image.new("RGBA", size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        odraw.rectangle((0, size[1] * .48, size[0], size[1]), fill=(0, 0, 0, 105))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(canvas)
        font_file = FONT_FILES.get(settings.cover.font, FONT_FILES["Microsoft YaHei"])
        if not font_file.exists():
            font_file = FONT_FILES["Microsoft YaHei"]
        scaled_size = max(28, round(settings.cover.font_size * size[0] / 1080))
        font = ImageFont.truetype(str(font_file), scaled_size)
        lines = wrap_title(draw, title, font, int(size[0] * .84))
        line_height = int(scaled_size * 1.2)
        y = int(size[1] * .66) - len(lines) * line_height // 2
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font, stroke_width=3)
            tw = box[2] - box[0]
            if settings.cover.title_align == "center":
                x = (size[0] - tw) // 2
            elif settings.cover.title_align == "left":
                x = int(size[0] * 0.08)
            else:
                x = int(size[0] * 0.92) - tw
            draw.text((x, y), line, font=font, fill=settings.cover.font_color,
                      stroke_width=max(2, scaled_size // 24), stroke_fill=settings.cover.stroke_color)
            y += line_height
        output = folder / f"★ 封面_{ratio.replace(':','x')}.jpg"
        canvas.convert("RGB").save(output, quality=94, subsampling=0)
        outputs.append(output)
    return outputs


def run_postprocess(settings: AppSettings, folder: Path, work_dir: Path, api_key: str,
                    seedream_key: str = "") -> dict:
    info = create_publication(settings, folder, api_key)
    covers = (create_covers(settings, folder, info.get("title", "纪录片解说"), work_dir,
                            seedream_key)
              if settings.cover.enabled else [])
    video = render_final(settings, folder, work_dir, api_key)
    return {"video": str(video), "covers": [str(x) for x in covers], "publication": info}
