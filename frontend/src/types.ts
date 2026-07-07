export type Settings = {
  material_folder: string
  ui: { language: 'zh' | 'en' }
  api: Record<string, string>
  video: {
    trim_head: number; trim_tail: number; padding_head: number; padding_tail: number
    separate_vocals_bgm: boolean; mute_source: boolean; source_volume: number
    target_minutes: number; resolution: string; video_crf: number; preset: string
    exclude_interviews: boolean
  }
  voice: {
    mode: string; provider: string; system_voice: string; clone_voice_id: string
    speech_rate: number; volume: number; pitch: number
    gpt_sovits_engine_path: string; gpt_sovits_reference_audio: string
    gpt_sovits_reference_text: string; polish_audio: boolean
  }
  narration: {
    style: string; custom_prompt: string; factual_strictness: number
    conversational_level: number; humor_level: number; allow_external_facts: boolean
  }
  subtitle: {
    enabled: boolean; font: string; size: number; color: string; border_color: string
    border_width: number; shadow: number; bottom_margin: number; max_chars_per_line: number
    single_line: boolean; ai_text_cleanup: boolean; time_offset: number
  }
  cover: {
    enabled: boolean; size: string; ratios: string[]; font: string; font_size: number
    font_color: string; stroke_color: string; title_align: 'left' | 'center' | 'right'
  }
  bgm: { mode: string; local_path: string; volume: number }
}

export type Material = {
  video_path: string; subtitle_paths: string[]; duration: number; width: number; height: number
  video_codec: string; audio_codec?: string; warnings: string[]
}

export type Job = {
  id: string; status: string; stage: string; progress: number; message: string
  output_path: string; error: string
  title: string; tags: string[]; description: string
  narration_text: string
}

export type SystemStats = {
  cpu_percent: number
  cpu_temperature: number | null
  memory_percent: number
  memory_used_gb: number
  memory_total_gb: number
  net_upload_bps: number
  net_download_bps: number
}
