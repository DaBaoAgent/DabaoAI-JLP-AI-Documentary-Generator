import { useEffect, useRef, useState } from 'react'
import {
  Activity, AudioLines, Captions, Check, Copy, FileUp, Film, FolderOpen,
  FolderSearch, Image, KeyRound, LoaderCircle, Music, Settings2,
  SlidersHorizontal, Sparkles, Square, Volume2, WandSparkles
} from 'lucide-react'
import type { Job, Material, Settings, SystemStats } from './types'
import './script-editor.css'

const BRAND_NAME = 'DabaoAI-JLP'
const APP_NAME = 'DabaoAI-JLP AI Documentary Video Generator'

type Lang = 'zh' | 'en'
type SectionId = 'material' | 'script' | 'subtitle' | 'cover' | 'bgm' | 'api'
type FontOption = { name: string; category: string; file?: string; kind?: string }

const sections: { id: SectionId; icon: typeof Film }[] = [
  { id: 'material', icon: Film },
  { id: 'script', icon: AudioLines },
  { id: 'subtitle', icon: Captions },
  { id: 'cover', icon: Image },
  { id: 'bgm', icon: Music },
  { id: 'api', icon: KeyRound },
]

const i18n = {
  zh: {
    subtitle: '纪录片全自动智能剪辑神器',
    loading: '正在启动 DabaoAI-JLP...',
    save: '保存参数',
    saved: '参数已保存到本机',
    localConnected: '本地服务已连接',
    finished: '成片完成',
    currentTask: '当前任务',
    ready: '准备就绪',
    readyMessage: '配置完成后即可开始智能成片。',
    start: '一键智能成片',
    startWithScript: '按当前文案成片',
    running: '正在智能成片',
    cancel: '取消任务',
    outputSaved: '成片已保存',
    publishTitle: '发布标题',
    tags: '精准标签',
    description: '视频描述',
    copy: '复制',
    log: '操作日志',
    requestFailed: '请求失败',
    apiSuccess: '连接成功',
    sections: {
      material: ['素材与成片', '粘贴素材目录，系统自动识别视频与字幕。'],
      script: ['文案与配音', '来源锚定文案，逐句克隆配音。'],
      subtitle: ['成片字幕', '设置烧录字幕的字体、颜色与位置。'],
      cover: ['封面生成', '一次生成多个平台比例，实时预览标题样式。'],
      bgm: ['背景音乐', '自动匹配合规曲库，或指定本机音乐。'],
      api: ['API 设置', '密钥仅保存在本机后端，页面不会显示明文。'],
    },
    nav: {
      material: '素材与时长',
      script: '文案与配音',
      subtitle: '字幕样式',
      cover: '封面生成',
      bgm: '背景音乐',
      api: 'API 设置',
    },
    stats: {
      title: '本机性能', temp: '温度', memory: '内存', used: '已用',
      download: '下载', upload: '上传', missing: '未检测',
    },
    material: {
      folder: '素材文件夹',
      placeholder: '例如：D:\\纪录片\\素材文件夹',
      pathTip: '路径提示',
      pathTipText: '浏览器不能读取本机绝对路径，请复制资源管理器地址栏里的完整文件夹路径后粘贴。',
      detect: '检测素材',
      detected: '已检测',
      minutes: '分钟',
      subtitles: '个字幕',
      trim: '原片裁切',
      trimHead: '跳过片头',
      trimTail: '跳过片尾',
      paddingHead: '成片片头缓冲',
      paddingTail: '成片片尾缓冲',
      output: '输出设置',
      target: '目标成片时长',
      targetHint: '用于规划文案篇幅，不会通过增加句间停顿或整体变速强行凑时长。',
      resolution: '输出分辨率',
      removeAudio: '去除原片人声和 BGM',
      mute: '原片完全静音',
      excludeInterviews: '去掉采访人物画面',
      sourceVolume: '原片音量',
      advanced: '高级编码设置',
      seconds: '秒',
    },
    script: {
      style: '解说风格',
      baseStyle: '基础风格',
      custom: '额外要求',
      customPlaceholder: '可填写本期纪录片的特殊要求',
      factual: '事实严谨',
      conversational: '口语程度',
      humor: '幽默程度',
      voice: '配音音色',
      system: '默认音色',
      clone: '百炼克隆音色',
      gpt: '本地 GPT-SoVITS',
      systemVoice: '百炼默认音色',
      referenceAudio: '被克隆音色的本机地址',
      referenceHint: '建议使用 3-10 秒、无背景音乐、声音清晰的 WAV 音频',
      referenceText: '参考音频对应文字',
      referenceTextHint: '必须与参考音频中说的话逐字一致',
      engine: '本地引擎地址',
      polish: '美化音色（提升音频质量）',
      gptInfo: 'GPT-SoVITS 默认自动使用 GPU；未检测到 CUDA 时才回落到 CPU。首次加载模型会稍慢。',
      testVoice: '测试配音',
      testingVoice: '正在生成测试配音，请稍候...',
      voiceReady: '测试配音已生成',
      voiceFailed: '测试失败',
      cloneId: '克隆音色 ID',
      speed: '语速',
      volume: '音量',
      pitch: '音高',
      fullScript: 'AI 完整口播文案',
      generate: 'AI生成文案',
      generating: '正在生成',
      scriptReady: '文案已生成，可在窗口中修改后按当前文案成片',
      scriptStart: '开始生成来源锚定口播文案，请稍候。',
      scriptDone: '完整文案已生成，可修改后按当前文案成片。',
      scriptFailed: '生成失败',
      emptyHint: '点击一键智能成片后，完整文案会先显示在这里',
      lineHelp: '每行代表一句口播。系统会在配音前自动生成朗读版临时文案，不影响最终字幕。',
      placeholder: 'AI 正在分析素材；文案生成后可在这里直接修改。每行代表一句口播，允许增删行。',
      pathTipText: '浏览器不能读取音频绝对路径，请右键音频文件复制完整路径后粘贴。',
      sentenceUnit: '句',
      charUnit: '字',
    },
    subtitlePanel: {
      burn: '烧录中文字幕',
      font: '字体',
      size: '字号',
      color: '字体颜色',
      borderColor: '描边颜色',
      borderWidth: '描边宽度',
      shadow: '阴影',
      margin: '底部边距',
      maxChars: '单行最大字数',
      preview: '在北极海域，猎手与猎物的身份正在悄然交换',
    },
    coverPanel: {
      enable: '生成封面',
      size: '封面尺寸',
      ratio: '封面比例',
      font: '标题字体',
      fontSize: '标题大小',
      fontColor: '标题颜色',
      stroke: '描边颜色',
      align: '标题位置',
      left: '左',
      center: '中',
      right: '右',
      previewTitle: '北角海战\n最后的追击',
      preview: '实时样式预览',
    },
    bgmPanel: {
      mode: 'BGM 模式',
      auto: 'AI 自动匹配',
      local: '指定本机 BGM',
      none: '不使用',
      path: '本机 BGM 路径',
      volume: 'BGM 音量',
      info: 'AI 自动匹配只使用可追溯授权来源，并在输出目录保存曲目与授权信息。',
    },
    api: {
      language: '界面语言',
      chinese: '中文',
      english: 'English',
      textKey: '文案 · DeepSeek',
      voiceKey: '配音 · 阿里百炼',
      visionKey: '视觉 · SiliconFlow',
      coverKey: '封面 · Seedream',
      test: '测试连接',
      textModel: '文案模型',
      visionModel: '视觉模型',
      coverModel: '封面模型',
    },
    colors: ['白', '黄', '红', '橙', '青', '绿', '蓝', '紫', '黑', '灰'],
    fontCategories: {
      current: '当前字体',
      system: '系统字体',
      english: '英文常用字幕',
      englishPoster: '英文标题字体',
      chinese: '中文常用字幕',
      chinesePoster: '中文标题字体',
    },
  },
  en: {
    subtitle: 'Fully Automated Documentary Editing Suite',
    loading: 'Starting DabaoAI-JLP...',
    save: 'Save Settings',
    saved: 'Settings saved locally',
    localConnected: 'Local service connected',
    finished: 'Video complete',
    currentTask: 'Current Task',
    ready: 'Ready',
    readyMessage: 'Start when the settings are complete.',
    start: 'Create Video',
    startWithScript: 'Create From Current Script',
    running: 'Creating Video',
    cancel: 'Cancel Task',
    outputSaved: 'Video saved',
    publishTitle: 'Publish Title',
    tags: 'Precise Tags',
    description: 'Description',
    copy: 'Copy',
    log: 'Activity Log',
    requestFailed: 'Request failed',
    apiSuccess: 'connected',
    sections: {
      material: ['Source & Runtime', 'Paste the source folder; the system detects video and subtitles.'],
      script: ['Script & Voice', 'Generate source-grounded narration and clone voice line by line.'],
      subtitle: ['Subtitles', 'Configure burned-in subtitle font, color, and position.'],
      cover: ['Cover Generation', 'Generate platform ratios and preview title styling.'],
      bgm: ['Background Music', 'Auto-match licensed tracks or use a local file.'],
      api: ['API Settings', 'Keys are stored only by the local backend and never shown in plain text.'],
    },
    nav: {
      material: 'Media & Length',
      script: 'Script & Voice',
      subtitle: 'Subtitle Style',
      cover: 'Cover',
      bgm: 'Music',
      api: 'API Settings',
    },
    stats: {
      title: 'System Monitor', temp: 'Temp', memory: 'Memory', used: 'Used',
      download: 'Download', upload: 'Upload', missing: 'N/A',
    },
    material: {
      folder: 'Source Folder',
      placeholder: 'Example: D:\\Documentary\\Source',
      pathTip: 'Path Tip',
      pathTipText: 'Browsers cannot read local absolute paths. Copy the full folder path from File Explorer and paste it here.',
      detect: 'Detect Media',
      detected: 'Detected',
      minutes: 'min',
      subtitles: 'subtitle files',
      trim: 'Source Trim',
      trimHead: 'Skip Opening',
      trimTail: 'Skip Ending',
      paddingHead: 'Output Head Padding',
      paddingTail: 'Output Tail Padding',
      output: 'Output',
      target: 'Target Runtime',
      targetHint: 'Used for script planning. The system will not force duration by adding pauses or stretching audio.',
      resolution: 'Resolution',
      removeAudio: 'Remove source vocals and BGM',
      mute: 'Mute source video',
      excludeInterviews: 'Exclude interview shots',
      sourceVolume: 'Source Volume',
      advanced: 'Advanced Encoding',
      seconds: 'sec',
    },
    script: {
      style: 'Narration Style',
      baseStyle: 'Base Style',
      custom: 'Extra Requirements',
      customPlaceholder: 'Optional constraints for this documentary',
      factual: 'Factual',
      conversational: 'Conversational',
      humor: 'Humor',
      voice: 'Voice',
      system: 'Default Voice',
      clone: 'Bailian Clone',
      gpt: 'Local GPT-SoVITS',
      systemVoice: 'Bailian Default Voice',
      referenceAudio: 'Reference Audio Path',
      referenceHint: 'Use a clear 3-10 second WAV with no background music',
      referenceText: 'Reference Text',
      referenceTextHint: 'Must exactly match the spoken reference audio',
      engine: 'Local Engine Path',
      polish: 'Polish voice quality',
      gptInfo: 'GPT-SoVITS uses GPU automatically when CUDA is available, otherwise CPU. The first model load may take longer.',
      testVoice: 'Test Voice',
      testingVoice: 'Generating test voice...',
      voiceReady: 'Test voice generated',
      voiceFailed: 'Voice test failed',
      cloneId: 'Clone Voice ID',
      speed: 'Speed',
      volume: 'Volume',
      pitch: 'Pitch',
      fullScript: 'Full AI Narration Script',
      generate: 'Generate Script',
      generating: 'Generating',
      scriptReady: 'Script generated. Edit it here, then create from the current script.',
      scriptStart: 'Generating source-grounded narration. Please wait.',
      scriptDone: 'Full script generated. You can edit it and create the video.',
      scriptFailed: 'Generation failed',
      emptyHint: 'After creating a video, the full script appears here first',
      lineHelp: 'Each line is one narration sentence. A temporary speech-friendly script is created for TTS without changing final subtitles.',
      placeholder: 'AI will analyze the source here. After generation, edit the narration directly. One line equals one spoken sentence.',
      pathTipText: 'Browsers cannot read local audio paths. Copy the full audio path and paste it here.',
      sentenceUnit: 'sentences',
      charUnit: 'chars',
    },
    subtitlePanel: {
      burn: 'Burn Chinese Subtitles',
      font: 'Font',
      size: 'Size',
      color: 'Text Color',
      borderColor: 'Stroke Color',
      borderWidth: 'Stroke Width',
      shadow: 'Shadow',
      margin: 'Bottom Margin',
      maxChars: 'Max Chars Per Line',
      preview: 'In Arctic waters, hunter and prey quietly trade places',
    },
    coverPanel: {
      enable: 'Generate Cover',
      size: 'Cover Size',
      ratio: 'Cover Ratio',
      font: 'Title Font',
      fontSize: 'Title Size',
      fontColor: 'Title Color',
      stroke: 'Stroke Color',
      align: 'Title Position',
      left: 'Left',
      center: 'Center',
      right: 'Right',
      previewTitle: 'North Cape\nThe Last Pursuit',
      preview: 'Live style preview',
    },
    bgmPanel: {
      mode: 'BGM Mode',
      auto: 'AI Auto Match',
      local: 'Local BGM',
      none: 'None',
      path: 'Local BGM Path',
      volume: 'BGM Volume',
      info: 'AI matching only uses traceable licensed sources and saves track and license info in the output folder.',
    },
    api: {
      language: 'Interface Language',
      chinese: '中文',
      english: 'English',
      textKey: 'Script · DeepSeek',
      voiceKey: 'Voice · Bailian',
      visionKey: 'Vision · SiliconFlow',
      coverKey: 'Cover · Seedream',
      test: 'Test',
      textModel: 'Script Model',
      visionModel: 'Vision Model',
      coverModel: 'Cover Model',
    },
    colors: ['White', 'Yellow', 'Red', 'Orange', 'Cyan', 'Green', 'Blue', 'Purple', 'Black', 'Gray'],
    fontCategories: {
      current: 'Current Font',
      system: 'System Fonts',
      english: 'Common English Subtitle Fonts',
      englishPoster: 'English Title Fonts',
      chinese: 'Common Chinese Subtitle Fonts',
      chinesePoster: 'Chinese Title Fonts',
    },
  },
} as const

const colorValues = ['#FFFFFF', '#FFD84D', '#FF5A5F', '#FF8A3D', '#59D5E0', '#58C878', '#5B8FF9', '#D58CFF', '#111111', '#666666'] as const

function fallbackFonts(lang: Lang): FontOption[] {
  const c = i18n[lang].fontCategories
  return [
    { name: 'Arial', category: c.english },
    { name: 'Calibri', category: c.english },
    { name: 'Verdana', category: c.english },
    { name: 'Comic Sans MS', category: c.englishPoster },
    { name: 'Impact', category: c.englishPoster },
    { name: 'Microsoft YaHei', category: c.chinese },
    { name: 'SimHei', category: c.chinese },
    { name: 'SimSun', category: c.chinese },
    { name: 'KaiTi', category: c.chinese },
    { name: 'DengXian', category: c.chinese },
    { name: 'Ma Shan Zheng', category: c.chinesePoster },
    { name: 'ZCOOL KuaiLe', category: c.chinesePoster },
    { name: 'ZCOOL XiaoWei', category: c.chinesePoster },
    { name: 'Long Cang', category: c.chinesePoster },
    { name: 'Liu Jian Mao Cao', category: c.chinesePoster },
  ]
}

async function jsonFetch(url: string, options?: RequestInit) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...(options?.headers || {}) } })
  const text = await response.text()
  const body = text ? (() => { try { return JSON.parse(text) } catch { return { detail: text } } })() : {}
  if (!response.ok) throw new Error(body.detail || 'Request failed')
  return body
}

function Field({ label, hint, children }: { label: string, hint?: string, children: React.ReactNode }) {
  return <label className="field"><span className="field-label">{label}</span>{children}{hint && <small>{hint}</small>}</label>
}

function Range({ value, min, max, step = 1, suffix = '', onChange }: {
  value: number, min: number, max: number, step?: number, suffix?: string, onChange: (v: number) => void
}) {
  return <div className="range-row"><input type="range" min={min} max={max} step={step} value={value}
    onChange={e => onChange(Number(e.target.value))}/><output>{value}{suffix}</output></div>
}

function Toggle({ checked, onChange, label }: { checked: boolean, onChange: (v: boolean) => void, label: string }) {
  return <button type="button" className={`toggle-row ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}>
    <span className="toggle"><i /></span><span>{label}</span>
  </button>
}

function ColorPalette({ value, lang, onChange }: { value: string, lang: Lang, onChange: (value: string) => void }) {
  return <div className="color-palette">{colorValues.map((color, index) => {
    const name = i18n[lang].colors[index]
    return <button type="button" key={color}
      className={value.toUpperCase() === color ? 'selected' : ''} style={{ backgroundColor: color }}
      title={name} aria-label={name} onClick={() => onChange(color)}>{value.toUpperCase() === color && <Check size={13}/>}</button>
  })}</div>
}

function FontSelect({ value, options, lang, onChange }: {
  value: string; options: FontOption[]; lang: Lang; onChange: (value: string) => void
}) {
  const visibleOptions = value && !options.some(font => font.name === value)
    ? [{ name: value, category: i18n[lang].fontCategories.current }, ...options]
    : options
  const groups = Array.from(new Set(visibleOptions.map(font => font.category)))
  return <select value={value} onChange={e => onChange(e.target.value)}>
    {groups.map(group => <optgroup key={group} label={group}>
      {visibleOptions.filter(font => font.category === group).map(font =>
        <option key={`${group}-${font.name}`} value={font.name}>{font.name}</option>
      )}
    </optgroup>)}
  </select>
}

export default function App() {
  const [settings, setSettings] = useState<Settings | null>(null)
  const lang: Lang = settings?.ui?.language || 'zh'
  const t = i18n[lang]
  const [active, setActive] = useState<SectionId>('material')
  const [material, setMaterial] = useState<Material | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [logs, setLogs] = useState<string[]>([`[system] ${APP_NAME} is ready. Waiting for source media.`])
  const [busy, setBusy] = useState(false)
  const [scriptBusy, setScriptBusy] = useState(false)
  const [voiceBusy, setVoiceBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [narrationText, setNarrationText] = useState('')
  const [audioUrl, setAudioUrl] = useState('')
  const [fonts, setFonts] = useState<FontOption[]>(fallbackFonts('zh'))
  const [stats, setStats] = useState<SystemStats | null>(null)
  const [systemVoices, setSystemVoices] = useState<{id:string, name:string}[]>([
    { id: 'Cherry', name: 'Cherry / 阳光自然' },
    { id: 'Serena', name: 'Serena / 温柔' },
    { id: 'Ethan', name: 'Ethan / 自然男声' },
  ])
  const logRef = useRef<HTMLDivElement>(null)
  const audioRef = useRef<HTMLAudioElement>(null)

  useEffect(() => { jsonFetch('/api/config').then(setSettings).catch(e => setNotice(e.message)) }, [])
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: 'smooth' }) }, [logs])
  useEffect(() => {
    jsonFetch('/api/fonts').then(data => {
      const list = Array.isArray(data) ? data : (data.fonts || [])
      if (list.length > 0) {
        setFonts(list.map((item: string | FontOption) =>
          typeof item === 'string' ? { name: item, category: i18n[lang].fontCategories.system } : item
        ))
      } else {
        setFonts(fallbackFonts(lang))
      }
    }).catch(() => setFonts(fallbackFonts(lang)))
  }, [lang])
  useEffect(() => { jsonFetch('/api/voices/list').then(data => {
    const list = Array.isArray(data) ? data : (data.voices || [])
    if (list.length > 0) setSystemVoices(list)
  }).catch(() => {}) }, [])
  useEffect(() => {
    const loadStats = () => jsonFetch('/api/system-stats').then(setStats).catch(() => {})
    loadStats()
    const timer = window.setInterval(loadStats, 2000)
    return () => window.clearInterval(timer)
  }, [])

  const formatSpeed = (value: number) => {
    if (!Number.isFinite(value) || value <= 0) return '0 KB/s'
    if (value >= 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB/s`
    return `${(value / 1024).toFixed(0)} KB/s`
  }

  const update = (group: keyof Settings, key: string, value: unknown) => setSettings(current => {
    if (!current) return current
    if (group === 'material_folder') return { ...current, material_folder: String(value) }
    return { ...current, [group]: { ...(current[group] as object), [key]: value } }
  })

  const detect = async () => {
    if (!settings) return
    setBusy(true); setNotice('')
    try {
      const result = await jsonFetch('/api/materials/detect', { method: 'POST', body: JSON.stringify({ folder: settings.material_folder }) })
      setMaterial(result)
      setLogs(x => [...x, `[${t.nav.material}] ${t.material.detected} ${result.width}x${result.height}, ${(result.duration / 60).toFixed(2)} ${t.material.minutes}.`])
    } catch (e) { setNotice((e as Error).message) } finally { setBusy(false) }
  }

  const start = async () => {
    if (!settings || running) return
    setBusy(true); setNotice(''); setLogs([])
    try {
      const created = await jsonFetch('/api/jobs', {
        method: 'POST',
        body: JSON.stringify({ settings, narration_text: narrationText.trim() })
      })
      setJob(created)
      const protocol = location.protocol === 'https:' ? 'wss' : 'ws'
      const socket = new WebSocket(`${protocol}://${location.host}/ws/jobs/${created.id}`)
      socket.onmessage = event => {
        const data = JSON.parse(event.data)
        if (data.type === 'log') setLogs(x => [...x, data.line])
        if (data.type === 'status') {
          setJob(data.job)
          if (data.job.narration_text) setNarrationText(data.job.narration_text)
          if (data.job.status === 'success') setBusy(false)
        }
      }
      socket.onclose = () => setBusy(false)
    } catch (e) { setNotice((e as Error).message); setBusy(false) }
  }

  const cancel = async () => { if (job) await jsonFetch(`/api/jobs/${job.id}/cancel`, { method: 'POST' }) }
  const save = async () => {
    if (!settings) return
    try { const saved = await jsonFetch('/api/config', { method: 'PUT', body: JSON.stringify(settings) }); setSettings(saved); setNotice(t.saved) }
    catch(e) { setNotice((e as Error).message) }
  }
  const testApi = async (provider: string, key: string) => {
    setNotice('')
    try { await jsonFetch('/api/api-test', { method: 'POST', body: JSON.stringify({ provider, key }) }); setNotice(`${provider} ${t.apiSuccess}`) }
    catch(e) { setNotice((e as Error).message) }
  }

  const testVoice = async () => {
    if (!settings) return
    setVoiceBusy(true); setNotice(t.script.testingVoice)
    try {
      const response = await fetch('/api/voices/test-gpt-sovits', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          reference_audio: settings.voice.gpt_sovits_reference_audio,
          reference_text: settings.voice.gpt_sovits_reference_text,
          engine_path: settings.voice.gpt_sovits_engine_path,
          speed: settings.voice.speech_rate,
          polish: settings.voice.polish_audio
        })
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(body.detail || t.script.voiceFailed)
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      if (audioUrl) URL.revokeObjectURL(audioUrl)
      setAudioUrl(url)
      setNotice(t.script.voiceReady)
    } catch(e) { setNotice((e as Error).message) } finally { setVoiceBusy(false) }
  }

  const generateScript = async () => {
    if (!settings?.material_folder || scriptBusy || running) return
    setScriptBusy(true)
    setNotice(t.script.generating)
    setLogs(x => [...x, `[${t.nav.script}] ${t.script.scriptStart}`])
    try {
      const result = await jsonFetch('/api/narration/generate', {
        method: 'POST',
        body: JSON.stringify({ settings })
      })
      setNarrationText(result.narration_text)
      setNotice(t.script.scriptReady)
      setLogs(x => [...x, `[${t.nav.script}] ${t.script.scriptDone}`])
    } catch(e) {
      const message = (e as Error).message
      setNotice(message)
      setLogs(x => [...x, `[${t.nav.script}] ${t.script.scriptFailed}: ${message}`])
    } finally {
      setScriptBusy(false)
    }
  }

  const running = job?.status === 'running' || job?.status === 'queued'
  const ratioPreview = settings?.cover.ratios[0] || '3:4'
  const currentFont = fonts.find(font => font.name === settings?.cover.font)
  const coverRatio = ratioPreview === '9:16' ? '9 / 16' : ratioPreview === '4:3' ? '4 / 3' : ratioPreview === '16:9' ? '16 / 9' : '3 / 4'

  if (!settings) return <div className="boot"><LoaderCircle className="spin"/> {t.loading}</div>

  const apiRows = [
    ['deepseek_api_key', t.api.textKey, 'deepseek'],
    ['dashscope_api_key', t.api.voiceKey, 'dashscope'],
    ['siliconflow_api_key', t.api.visionKey, 'siliconflow'],
    ['seedream_api_key', t.api.coverKey, 'seedream'],
  ] as const

  return <div className="app-shell">
    <header><div className="brand"><div className="brand-mark"><img src="/icon.png" alt={APP_NAME} style={{width:36,height:36,borderRadius:8}}/></div>
      <div><h1>{BRAND_NAME}</h1><p>{t.subtitle}</p></div></div>
      <div className="header-actions"><button onClick={save}><Settings2 size={14}/>{t.save}</button><div className={`status-pill ${running ? 'working' : job?.status || ''}`}><Activity size={14}/>
        {running ? job?.stage : job?.status === 'success' ? t.finished : t.localConnected}</div></div></header>

    <main><aside>{sections.map(({ id, icon: Icon }) => <button key={id} className={active === id ? 'active' : ''}
      onClick={() => setActive(id)}><Icon size={17}/>{t.nav[id]}</button>)}
      <div className="system-panel">
        <div className="system-panel-title"><Activity size={14}/>{t.stats.title}</div>
        <div className="metric"><span>CPU</span><b>{stats ? `${stats.cpu_percent}%` : '--'}</b></div>
        <div className="metric"><span>{t.stats.temp}</span><b>{stats?.cpu_temperature == null ? t.stats.missing : `${stats.cpu_temperature}°C`}</b></div>
        <div className="metric"><span>{t.stats.memory}</span><b>{stats ? `${stats.memory_percent}%` : '--'}</b></div>
        <div className="metric small"><span>{t.stats.used}</span><b>{stats ? `${stats.memory_used_gb}/${stats.memory_total_gb} GB` : '--'}</b></div>
        <div className="metric small"><span>{t.stats.download}</span><b>{stats ? formatSpeed(stats.net_download_bps) : '--'}</b></div>
        <div className="metric small"><span>{t.stats.upload}</span><b>{stats ? formatSpeed(stats.net_upload_bps) : '--'}</b></div>
      </div>
    </aside>

      <section className="workspace"><div className="workspace-main">
        {active === 'material' && <>
          <div className="section-title"><div><span>01</span><h2>{t.sections.material[0]}</h2></div><p>{t.sections.material[1]}</p></div>
          <div className="card hero-card"><Field label={t.material.folder}>
            <div className="path-input"><input value={settings.material_folder} placeholder={t.material.placeholder}
              onChange={e => update('material_folder', '', e.target.value)}/>
              <button type="button" onClick={() => setNotice(t.material.pathTipText)}><FolderOpen size={17}/>{t.material.pathTip}</button>
              <button onClick={detect} disabled={busy}><FolderSearch size={17}/>{t.material.detect}</button></div>
          </Field>{material && <div className="material-result"><Check size={18}/><div><b>{material.video_path.split(/[\\/]/).pop()}</b>
            <span>{material.width}x{material.height} · {(material.duration / 60).toFixed(2)} {t.material.minutes} · {material.video_codec.toUpperCase()} · {material.subtitle_paths.length} {t.material.subtitles}</span></div></div>}</div>
          <div className="grid two"><div className="card"><h3>{t.material.trim}</h3>
            <Field label={t.material.trimHead}><Range value={settings.video.trim_head} min={1} max={300} suffix={` ${t.material.seconds}`} onChange={v => update('video','trim_head',v)}/></Field>
            <Field label={t.material.trimTail}><Range value={settings.video.trim_tail} min={1} max={300} suffix={` ${t.material.seconds}`} onChange={v => update('video','trim_tail',v)}/></Field>
            <Field label={t.material.paddingHead}><Range value={settings.video.padding_head} min={0} max={5} step={0.5} suffix={` ${t.material.seconds}`} onChange={v => update('video','padding_head',v)}/></Field>
            <Field label={t.material.paddingTail}><Range value={settings.video.padding_tail} min={0} max={5} step={0.5} suffix={` ${t.material.seconds}`} onChange={v => update('video','padding_tail',v)}/></Field>
          </div><div className="card"><h3>{t.material.output}</h3>
            <Field label={t.material.target} hint={t.material.targetHint}><Range value={settings.video.target_minutes} min={5} max={60} suffix={` ${t.material.minutes}`} onChange={v => update('video','target_minutes',v)}/></Field>
            <Field label={t.material.resolution}><select value={settings.video.resolution} onChange={e => update('video','resolution',e.target.value)}>{['720P','1080P','2K','4K'].map(x=><option key={x}>{x}</option>)}</select></Field>
            <Toggle checked={settings.video.separate_vocals_bgm} onChange={v=>update('video','separate_vocals_bgm',v)} label={t.material.removeAudio}/>
            <Toggle checked={settings.video.mute_source} onChange={v=>update('video','mute_source',v)} label={t.material.mute}/>
            <Toggle checked={settings.video.exclude_interviews} onChange={v=>update('video','exclude_interviews',v)} label={t.material.excludeInterviews}/>
            <Field label={t.material.sourceVolume}><Range value={settings.video.source_volume} min={0} max={1.5} step={0.05} suffix="x" onChange={v=>update('video','source_volume',v)}/></Field>
            <details><summary><Settings2 size={15}/>{t.material.advanced}</summary><Field label="CRF"><Range value={settings.video.video_crf} min={14} max={32} onChange={v=>update('video','video_crf',v)}/></Field></details>
          </div></div>
        </>}

        {active === 'script' && <><div className="section-title"><div><span>02</span><h2>{t.sections.script[0]}</h2></div><p>{t.sections.script[1]}</p></div>
          <div className="card"><h3>{t.script.style}</h3><Field label={t.script.baseStyle}><textarea rows={3} value={settings.narration.style} onChange={e=>update('narration','style',e.target.value)}/></Field>
            <Field label={t.script.custom}><textarea rows={3} placeholder={t.script.customPlaceholder} value={settings.narration.custom_prompt} onChange={e=>update('narration','custom_prompt',e.target.value)}/></Field>
            <div className="grid three"><Field label={t.script.factual}><Range value={settings.narration.factual_strictness} min={0} max={100} suffix="%" onChange={v=>update('narration','factual_strictness',v)}/></Field>
              <Field label={t.script.conversational}><Range value={settings.narration.conversational_level} min={0} max={100} suffix="%" onChange={v=>update('narration','conversational_level',v)}/></Field>
              <Field label={t.script.humor}><Range value={settings.narration.humor_level} min={0} max={100} suffix="%" onChange={v=>update('narration','humor_level',v)}/></Field></div></div>
          <div className="card"><h3>{t.script.voice}</h3><div className="segmented"><button className={settings.voice.mode==='system'?'selected':''} onClick={()=>update('voice','mode','system')}>{t.script.system}</button><button className={settings.voice.mode==='clone'&&settings.voice.provider!=='gpt_sovits'?'selected':''} onClick={()=>{update('voice','mode','clone');update('voice','provider','qwen')}}>{t.script.clone}</button><button className={settings.voice.mode==='clone'&&settings.voice.provider==='gpt_sovits'?'selected':''} onClick={()=>{update('voice','mode','clone');update('voice','provider','gpt_sovits');update('voice','speech_rate',1.2)}}>{t.script.gpt}</button></div>
            {settings.voice.mode==='system'?<Field label={t.script.systemVoice}><select value={settings.voice.system_voice} onChange={e=>update('voice','system_voice',e.target.value)}>{systemVoices.map(v=><option key={v.id} value={v.id}>{v.name}</option>)}</select></Field>:
              settings.voice.provider==='gpt_sovits'?<>
                <Field label={t.script.referenceAudio} hint={t.script.referenceHint}>
                  <div className="path-input"><input placeholder="D:\voice\reference.wav" value={settings.voice.gpt_sovits_reference_audio} onChange={e=>update('voice','gpt_sovits_reference_audio',e.target.value)}/>
                    <button type="button" onClick={() => setNotice(t.script.pathTipText)}><FileUp size={17}/>{t.material.pathTip}</button></div>
                </Field>
                <Field label={t.script.referenceText} hint={t.script.referenceTextHint}><textarea rows={3} value={settings.voice.gpt_sovits_reference_text} onChange={e=>update('voice','gpt_sovits_reference_text',e.target.value)}/></Field>
                <Field label={t.script.engine}><input value={settings.voice.gpt_sovits_engine_path} onChange={e=>update('voice','gpt_sovits_engine_path',e.target.value)}/></Field>
                <Toggle checked={settings.voice.polish_audio} onChange={v=>update('voice','polish_audio',v)} label={t.script.polish}/>
                <div className="info-box">{t.script.gptInfo}</div>
                <button className="primary" style={{marginTop:12}} onClick={testVoice} disabled={voiceBusy || !settings.voice.gpt_sovits_reference_audio || !settings.voice.gpt_sovits_engine_path}>{voiceBusy ? <LoaderCircle className="spin" size={14}/> : <Volume2 size={14}/>} {t.script.testVoice}</button>
                {audioUrl && <div className="audio-player"><audio controls ref={audioRef} src={audioUrl}/></div>}</>:
              <Field label={t.script.cloneId}><input value={settings.voice.clone_voice_id} onChange={e=>update('voice','clone_voice_id',e.target.value)}/></Field>}
            <div className="grid three"><Field label={t.script.speed}><Range value={settings.voice.speech_rate} min={0.7} max={1.5} step={0.1} suffix="x" onChange={v=>update('voice','speech_rate',v)}/></Field>
              <Field label={t.script.volume}><Range value={settings.voice.volume} min={0} max={100} suffix="%" onChange={v=>update('voice','volume',v)}/></Field>
              <Field label={t.script.pitch}><Range value={settings.voice.pitch} min={0.5} max={2} step={0.1} suffix="x" onChange={v=>update('voice','pitch',v)}/></Field></div></div>
          <div className={`card script-editor`}><div className="script-editor-head">
            <div><h3>{t.script.fullScript}</h3><small>{narrationText ? `${narrationText.split(/\n/).filter(Boolean).length} ${t.script.sentenceUnit} · ${narrationText.replace(/\s/g,'').length} ${t.script.charUnit}` : t.script.emptyHint}</small></div>
            <div style={{display:'flex', gap:6, alignItems:'center'}}>
              <button onClick={generateScript} disabled={scriptBusy || running || !settings.material_folder}>
                {scriptBusy ? <LoaderCircle className="spin" size={14}/> : <Sparkles size={14}/>}
                {scriptBusy ? t.script.generating : t.script.generate}
              </button>
              {narrationText && <button onClick={() => navigator.clipboard.writeText(narrationText)}><Copy size={14}/>{t.copy}</button>}
            </div></div>
            <textarea rows={18} value={narrationText} placeholder={t.script.placeholder} onChange={e=>setNarrationText(e.target.value)}/>
            <p>{t.script.lineHelp}</p></div>
        </>}

        {active === 'subtitle' && <><div className="section-title"><div><span>03</span><h2>{t.sections.subtitle[0]}</h2></div><p>{t.sections.subtitle[1]}</p></div>
          <div className="card"><Toggle checked={settings.subtitle.enabled} onChange={v=>update('subtitle','enabled',v)} label={t.subtitlePanel.burn}/>
            <div className="grid two"><Field label={t.subtitlePanel.font}><FontSelect value={settings.subtitle.font} options={fonts} lang={lang} onChange={v=>update('subtitle','font',v)}/></Field>
              <Field label={t.subtitlePanel.size}><Range value={settings.subtitle.size} min={20} max={120} onChange={v=>update('subtitle','size',v)}/></Field>
              <Field label={t.subtitlePanel.color}><ColorPalette value={settings.subtitle.color} lang={lang} onChange={v=>update('subtitle','color',v)}/></Field>
              <Field label={t.subtitlePanel.borderColor}><ColorPalette value={settings.subtitle.border_color} lang={lang} onChange={v=>update('subtitle','border_color',v)}/></Field>
              <Field label={t.subtitlePanel.borderWidth}><Range value={settings.subtitle.border_width} min={0} max={12} onChange={v=>update('subtitle','border_width',v)}/></Field>
              <Field label={t.subtitlePanel.shadow}><Range value={settings.subtitle.shadow} min={0} max={10} onChange={v=>update('subtitle','shadow',v)}/></Field>
              <Field label={t.subtitlePanel.margin}><Range value={settings.subtitle.bottom_margin} min={0} max={400} suffix=" px" onChange={v=>update('subtitle','bottom_margin',v)}/></Field>
              <Field label={t.subtitlePanel.maxChars}><Range value={settings.subtitle.max_chars_per_line} min={8} max={40} onChange={v=>update('subtitle','max_chars_per_line',v)}/></Field></div>
            <div className="subtitle-preview-frame">
              <div className="subtitle-preview" style={{
                fontFamily: `"${settings.subtitle.font}", sans-serif`,
                fontSize: settings.subtitle.size / 2,
                color: settings.subtitle.color,
                WebkitTextStroke: `${Math.max(1, settings.subtitle.border_width / 2)}px ${settings.subtitle.border_color}`,
                textShadow: settings.subtitle.shadow ? `0 ${settings.subtitle.shadow}px ${settings.subtitle.shadow * 3}px rgba(0,0,0,.75)` : 'none',
                marginBottom: Math.max(10, settings.subtitle.bottom_margin / 5)
              }}>{t.subtitlePanel.preview}</div>
            </div></div>
        </>}

        {active === 'cover' && <><div className="section-title"><div><span>04</span><h2>{t.sections.cover[0]}</h2></div><p>{t.sections.cover[1]}</p></div>
          <div className="grid cover-grid"><div className="card"><Toggle checked={settings.cover.enabled} onChange={v=>update('cover','enabled',v)} label={t.coverPanel.enable}/>
            <Field label={t.coverPanel.size}><select value={settings.cover.size} onChange={e=>update('cover','size',e.target.value)}><option>720P</option><option>1080P</option></select></Field>
            <Field label={t.coverPanel.ratio}><div className="checks">{['3:4','9:16','4:3','16:9'].map(r=><button key={r} className={settings.cover.ratios.includes(r)?'checked':''} onClick={()=>update('cover','ratios',settings.cover.ratios.includes(r)?settings.cover.ratios.filter(x=>x!==r):[...settings.cover.ratios,r])}>{settings.cover.ratios.includes(r)&&<Check size={14}/>} {r}</button>)}</div></Field>
            <Field label={t.coverPanel.font}><FontSelect value={settings.cover.font} options={fonts} lang={lang} onChange={v=>update('cover','font',v)}/></Field>
            <Field label={t.coverPanel.fontSize}><Range value={settings.cover.font_size} min={30} max={220} onChange={v=>update('cover','font_size',v)}/></Field>
            <Field label={t.coverPanel.fontColor}><ColorPalette value={settings.cover.font_color} lang={lang} onChange={v=>update('cover','font_color',v)}/></Field><Field label={t.coverPanel.stroke}><ColorPalette value={settings.cover.stroke_color} lang={lang} onChange={v=>update('cover','stroke_color',v)}/></Field>
            <Field label={t.coverPanel.align}><div className="segmented">{(['left','center','right'] as const).map(a=><button key={a} className={settings.cover.title_align===a?'selected':''} onClick={()=>update('cover','title_align',a)}>{t.coverPanel[a]}</button>)}</div></Field></div>
            <div className="cover-preview"><div className="cover-art" style={{aspectRatio: coverRatio}}>
              <Sparkles/>
              <span style={{
                fontFamily: `"${settings.cover.font}", sans-serif`,
                fontSize: Math.max(24, settings.cover.font_size / 2),
                color: settings.cover.font_color,
                WebkitTextStroke: `2px ${settings.cover.stroke_color}`,
                textAlign: settings.cover.title_align,
                alignSelf: settings.cover.title_align === 'left' ? 'flex-start' : settings.cover.title_align === 'right' ? 'flex-end' : 'center'
              }}>{t.coverPanel.previewTitle.split('\n').map((line, index) => <span key={line}>{index > 0 && <br/>}{line}</span>)}</span>
              <small>{ratioPreview} · {currentFont?.category || t.subtitlePanel.font} {t.coverPanel.preview}</small>
            </div></div></div>
        </>}

        {active === 'bgm' && <><div className="section-title"><div><span>05</span><h2>{t.sections.bgm[0]}</h2></div><p>{t.sections.bgm[1]}</p></div>
          <div className="card"><Field label={t.bgmPanel.mode}><div className="segmented">{[['auto',t.bgmPanel.auto],['local',t.bgmPanel.local],['none',t.bgmPanel.none]].map(([v,n])=><button key={v} className={settings.bgm.mode===v?'selected':''} onClick={()=>update('bgm','mode',v)}>{n}</button>)}</div></Field>
            {settings.bgm.mode==='local'&&<Field label={t.bgmPanel.path}><input placeholder="D:\music\documentary.mp3" value={settings.bgm.local_path} onChange={e=>update('bgm','local_path',e.target.value)}/></Field>}
            {settings.bgm.mode!=='none'&&<Field label={t.bgmPanel.volume}><Range value={Math.round(settings.bgm.volume*100)} min={5} max={100} suffix="%" onChange={v=>update('bgm','volume',v/100)}/></Field>}
            <div className="info-box">{t.bgmPanel.info}</div></div>
        </>}

        {active === 'api' && <><div className="section-title"><div><span>06</span><h2>{t.sections.api[0]}</h2></div><p>{t.sections.api[1]}</p></div>
          <div className="card api-card">
            <Field label={t.api.language}><div className="segmented language-toggle">
              <button className={settings.ui.language === 'zh' ? 'selected' : ''} onClick={() => update('ui','language','zh')}>{t.api.chinese}</button>
              <button className={settings.ui.language === 'en' ? 'selected' : ''} onClick={() => update('ui','language','en')}>{t.api.english}</button>
            </div></Field>
            {apiRows.map(([key,label,provider])=><Field key={key} label={label}><div className="secret"><input type="password" value={settings.api[key]||''} onChange={e=>update('api',key,e.target.value)}/><button onClick={()=>testApi(provider,settings.api[key]||'')}>{t.api.test}</button></div></Field>)}
            <Field label={t.api.textModel}><input value={settings.api.deepseek_model||''} onChange={e=>update('api','deepseek_model',e.target.value)}/></Field>
            <Field label={t.api.visionModel}><input value={settings.api.visual_model||''} onChange={e=>update('api','visual_model',e.target.value)}/></Field>
            <Field label={t.api.coverModel}><input value={settings.api.cover_model||''} onChange={e=>update('api','cover_model',e.target.value)}/></Field></div>
        </>}
      </div>

      <div className="workspace-side"><div className="run-card"><div className="run-head"><div><small>{t.currentTask}</small><b>{job?.stage || t.ready}</b></div><span>{job?.progress || 0}%</span></div>
        <div className="progress"><i style={{width:`${job?.progress||0}%`}}/></div><p>{job?.message || t.readyMessage}</p>
        {!running ? <button className="primary" onClick={start} disabled={busy || scriptBusy}><WandSparkles size={19}/>{narrationText.trim() ? t.startWithScript : t.start}</button> :
          <button className="primary running" disabled><LoaderCircle className="spin" size={19}/>{t.running}</button>}
        {running && <button className="cancel" onClick={cancel}><Square size={14}/>{t.cancel}</button>}
        {job?.status==='success'&&<div className="success-output"><Check/>{t.outputSaved}<br/><small>{job.output_path}</small></div>}
        {job?.status==='success'&&job.title&&<div className="publish-result"><div><b>{t.publishTitle}</b><button onClick={()=>navigator.clipboard.writeText(job.title)}><Copy size={12}/></button></div><p>{job.title}</p>
          <div><b>{t.tags}</b><button onClick={()=>navigator.clipboard.writeText(job.tags.map(x=>'#'+x).join(' '))}><Copy size={12}/></button></div><p>{job.tags.map(x=>'#'+x).join(' ')}</p>
          <div><b>{t.description}</b><button onClick={()=>navigator.clipboard.writeText(job.description)}><Copy size={12}/></button></div><p>{job.description}</p></div>}
      </div>
      {notice && <div className="notice">{notice}</div>}
      <div className="log-card"><div className="log-head"><span><SlidersHorizontal size={15}/>{t.log}</span><button onClick={()=>navigator.clipboard.writeText(logs.join('\n'))}><Copy size={14}/>{t.copy}</button></div>
        <div className="logs" ref={logRef}>{logs.map((x,i)=><div key={i} className={x.includes('失败') || x.toLowerCase().includes('failed') ? 'error' : x.includes('完成') || x.toLowerCase().includes('done') ? 'success' : ''}>{x}</div>)}</div></div>
      </div></section>
    </main>
  </div>
}
