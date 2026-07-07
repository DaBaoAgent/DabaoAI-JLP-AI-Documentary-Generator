from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .media_tools import ffprobe
from .schemas import MaterialInfo


def detect_materials(folder_value: str) -> MaterialInfo:
    folder = Path(folder_value.strip().strip('"')).expanduser()
    if not folder.is_dir():
        raise ValueError(f"素材文件夹不存在：{folder}")

    videos = sorted(
        [*folder.glob("*.mp4"), *folder.glob("*.mkv"), *folder.glob("*.mov")],
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    subtitles = sorted(folder.glob("*.srt"))
    if not videos:
        raise ValueError("素材文件夹内没有 MP4/MKV/MOV 视频")
    if not subtitles:
        raise ValueError("素材文件夹内没有 SRT 字幕")

    video = videos[0]
    try:
        probe = subprocess.run(
            [
                ffprobe(),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(video),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc))[-1200:]
        raise RuntimeError(f"ffprobe 读取视频失败：{detail}") from exc

    data = json.loads(probe.stdout)
    vstream = next(x for x in data["streams"] if x.get("codec_type") == "video")
    astream = next((x for x in data["streams"] if x.get("codec_type") == "audio"), None)
    warnings = []
    if len(videos) > 1:
        warnings.append(f"检测到 {len(videos)} 个视频，默认选择体积最大的文件")
    if len(subtitles) == 1:
        warnings.append("仅检测到一个字幕文件，将自动识别语言")

    return MaterialInfo(
        folder=str(folder.resolve()),
        video_path=str(video.resolve()),
        subtitle_paths=[str(x.resolve()) for x in subtitles],
        duration=float(data["format"]["duration"]),
        width=int(vstream["width"]),
        height=int(vstream["height"]),
        video_codec=vstream.get("codec_name", "unknown"),
        audio_codec=astream.get("codec_name") if astream else None,
        warnings=warnings,
    )
