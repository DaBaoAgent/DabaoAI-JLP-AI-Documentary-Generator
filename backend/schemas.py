from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ApiSettings(BaseModel):
    deepseek_api_key: str = ""
    dashscope_api_key: str = ""
    siliconflow_api_key: str = ""
    seedream_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    visual_model: str = "Qwen/Qwen3-VL-32B-Instruct"
    cover_model: str = "doubao-seedream-4-0-250828"


class UiSettings(BaseModel):
    language: Literal["zh", "en"] = "en"


class SubtitleStyle(BaseModel):
    enabled: bool = True
    font: str = "Microsoft YaHei"
    size: int = Field(52, ge=20, le=120)
    color: str = "#FFFFFF"
    border_color: str = "#000000"
    border_width: int = Field(3, ge=0, le=12)
    shadow: int = Field(1, ge=0, le=10)
    bottom_margin: int = Field(70, ge=0, le=400)
    max_chars_per_line: int = Field(18, ge=8, le=40)
    single_line: bool = True
    ai_text_cleanup: bool = True
    time_offset: float = Field(0.0, ge=-5.0, le=5.0)


class CoverSettings(BaseModel):
    enabled: bool = False
    size: Literal["720P", "1080P"] = "1080P"
    ratios: list[Literal["3:4", "9:16", "4:3", "16:9"]] = ["3:4"]
    font: str = "Ma Shan Zheng"
    font_size: int = Field(92, ge=30, le=220)
    font_color: str = "#FFFFFF"
    stroke_color: str = "#111111"
    title_align: Literal["left", "center", "right"] = "center"


class BgmSettings(BaseModel):
    mode: Literal["auto", "local", "none"] = "auto"
    local_path: str = ""
    volume: float = Field(0.1, ge=0.05, le=1.0)


class VoiceSettings(BaseModel):
    mode: Literal["system", "clone"] = "clone"
    provider: Literal["qwen", "cosyvoice", "gpt_sovits"] = "qwen"
    system_voice: str = "Cherry"
    clone_voice_id: str = ""
    speech_rate: float = Field(1.0, ge=0.7, le=1.5)
    volume: int = Field(55, ge=0, le=100)
    pitch: float = Field(1.0, ge=0.5, le=2.0)
    gpt_sovits_engine_path: str = ""
    gpt_sovits_reference_audio: str = ""
    gpt_sovits_reference_text: str = ""
    polish_audio: bool = False


class VideoSettings(BaseModel):
    trim_head: int = Field(6, ge=1, le=300)
    trim_tail: int = Field(15, ge=1, le=300)
    padding_head: float = Field(1.0, ge=0, le=5)
    padding_tail: float = Field(3.0, ge=0, le=5)
    separate_vocals_bgm: bool = True
    mute_source: bool = False
    source_volume: float = Field(0.5, ge=0, le=1.5)
    target_minutes: int = Field(10, ge=5, le=60)
    resolution: Literal["720P", "1080P", "2K", "4K"] = "1080P"
    video_crf: int = Field(20, ge=14, le=32)
    preset: Literal["fast", "medium", "slow"] = "fast"
    exclude_interviews: bool = True


class NarrationSettings(BaseModel):
    style: str = "Cinematic documentary narration with factual structure, clear pacing, and a conversational voice."
    custom_prompt: str = ""
    factual_strictness: int = Field(90, ge=0, le=100)
    conversational_level: int = Field(65, ge=0, le=100)
    humor_level: int = Field(20, ge=0, le=100)
    allow_external_facts: bool = False


class AppSettings(BaseModel):
    material_folder: str = ""
    ui: UiSettings = UiSettings()
    api: ApiSettings = ApiSettings()
    video: VideoSettings = VideoSettings()
    voice: VoiceSettings = VoiceSettings()
    narration: NarrationSettings = NarrationSettings()
    subtitle: SubtitleStyle = SubtitleStyle()
    cover: CoverSettings = CoverSettings()
    bgm: BgmSettings = BgmSettings()

    @model_validator(mode="after")
    def normalize_audio_options(self):
        if self.video.mute_source:
            self.video.source_volume = 0
        return self


class MaterialInfo(BaseModel):
    folder: str
    video_path: str
    subtitle_paths: list[str]
    duration: float
    width: int
    height: int
    video_codec: str
    audio_codec: str | None = None
    warnings: list[str] = []


class JobCreate(BaseModel):
    settings: AppSettings


class JobInfo(BaseModel):
    id: str
    status: Literal["queued", "running", "success", "failed", "cancelled"]
    stage: str = ""
    progress: int = 0
    message: str = ""
    output_path: str = ""
    error: str = ""
    title: str = ""
    tags: list[str] = []
    description: str = ""
    narration_text: str = ""


class JobContinue(BaseModel):
    narration_text: str
