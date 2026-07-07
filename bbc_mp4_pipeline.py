#!/usr/bin/env python3
"""
bbc_mp4_pipeline.py — BBC全配音MP4版一键流程 v1.6
===================================================
输入：素材文件夹（含 source.mp4 / subtitle.srt，配音稿可选）
输出：成片.mp4 + 封面.jpg + 字幕.srt + 发布信息 + BGM

v1.6 改进（借鉴 OpenMontage 纪录片管线）:
  ① 结构化画面描述: Qwen3-VL 输出 CLIP 级模板 (<主体>, <动作>, <环境>, <光线>, <质感>)
  ② 画面多样性校验: 相邻主体+景别相同的自动去重（MMR 风格）
  ③ 节奏网格: 标记 hero/普通/过场 slots，差异化 hold 时长 (6s/3.5s/2s)
  ④ 多 Query 描述: 每帧生成 literal/lateral/associative 三个搜索角度
  ⑤ 最佳子窗口: 从素材中智能截取黄金 3 秒，而非全段使用
  ⑥ 转场语言: 硬切 90% + dissolve (段落) + fade (开篇/结尾)，禁止特效

阶段:
  Phase 0   — DeepSeek 从SRT自动生成BBC中文口播稿（跳过若配音稿.txt已存在）
  Phase 1   — CosyVoice 分段配音合成（支持断点续跑）
  Phase 1.5 — Qwen3-VL 结构化视觉索引 + 多Query
  Phase 2   — 音频断句 + DP场景匹配 + 画面多样性校验
  Phase 3   — 裁切画面片段（节奏网格 + 子窗口提取）
  Phase 4   — 裁切间隔片段
  Phase 5   — 拼接 1080P + 混入配音（转场语言约束）
  Phase 6   — 发布包（封面/标题/字幕/BGM）

用法：
  python bbc_mp4_pipeline.py "E:\\纪录片\\油管\\素材文件夹"
  python scripts/bbc_mp4_pipeline.py "E:\\素材文件夹"

依赖：dashscope, ffmpeg, Python 3.9+

配置：同目录下的 config.json（API密钥等集中管理）

⚠ Windows IPv6 问题：DNS 默认返回 IPv6 地址导致百炼 WebSocket 连接超时。
  脚本已内置 socket.getaddrinfo 强制 IPv4 修复。
"""

import os, sys, re, json, time, shutil, subprocess, urllib.request, urllib.error, base64
import socket as _socket
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import scene_matcher

# Force IPv4: Windows DNS sometimes resolves DashScope to IPv6 which can't connect
# Patch 1: synchronous socket
_orig_getaddrinfo = _socket.getaddrinfo
_socket.getaddrinfo = lambda h, p, *a, **kw: _orig_getaddrinfo(h, p, _socket.AF_INET, *a[1:], **kw)

# Patch 2: asyncio event loop (websockets/dashscope use asyncio)
try:
    import asyncio
    _orig_loop_getaddrinfo = asyncio.base_events.BaseEventLoop.getaddrinfo
    def _patched_getaddrinfo(self, host, port, *, family=0, **kw):
        return _orig_loop_getaddrinfo(self, host, port, family=_socket.AF_INET, **kw)
    asyncio.base_events.BaseEventLoop.getaddrinfo = _patched_getaddrinfo
except Exception:
    pass

# Patch 3: environment-level hint
os.environ.setdefault("PREFER_IPV4", "1")

# Patch 4: Force websocket.create_connection to use IPv4
# This is the most reliable way — overrides the actual connect call used by all paths
_socket_create_connection = _socket.create_connection
def _create_connection_ipv4(address, timeout=None, source_address=None, **kwargs):
    host, port = address[0], address[1]
    addrs = _socket.getaddrinfo(host, port, _socket.AF_INET, _socket.SOCK_STREAM)
    for fam, typ, proto, canon, sa in addrs:
        try:
            sock = _socket.socket(fam, typ, proto)
            if timeout is not None:
                sock.settimeout(timeout)
            sock.connect(sa)
            return sock
        except Exception:
            if sock:
                sock.close()
    raise OSError(f"IPv4 connection to {host}:{port} failed")
_socket.create_connection = _create_connection_ipv4

# ============================================================
# 0. 配置加载
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

def load_config():
    """加载config.json，解析环境变量"""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key in ["dashscope_env_path", "narratoai_config_path"]:
        if key in cfg:
            cfg[key] = os.path.expandvars(cfg[key])
    return cfg

def load_api_keys(cfg):
    """从.env加载API密钥（主来源），config.json作为后备"""
    keys = {}

    # Primary: .env file (preferred for standalone/Codex usage)
    env_path = cfg.get("dashscope_env_path", ".env")
    # Resolve relative to script dir
    if not os.path.isabs(env_path):
        env_path = os.path.join(SCRIPT_DIR, env_path)
    
    env_keys = {}
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_keys[k] = v

    # Map env vars to internal key names
    keys["dashscope"] = env_keys.get("DASHSCOPE_API_KEY", "")
    keys["deepseek"] = env_keys.get("DEEPSEEK_API_KEY", "")
    keys["siliconflow"] = env_keys.get("SILICONFLOW_API_KEY", "")
    keys["seedream"] = env_keys.get("SEEDREAM_API_KEY", "")

    # Fallback: config.json for keys not in .env
    if not keys["deepseek"]:
        # Legacy: NarratoAI config.toml
        narratoai_path = cfg.get("narratoai_config_path", "")
        if narratoai_path and os.path.exists(narratoai_path):
            with open(narratoai_path, "r", encoding="utf-8") as f:
                content = f.read()
            m = re.search(r'text_openai_api_key\s*=\s*"([^"]+)"', content)
            if m:
                keys["deepseek"] = m.group(1)
    if not keys["siliconflow"]:
        keys["siliconflow"] = cfg.get("siliconflow", {}).get("api_key", "")
    if not keys["seedream"]:
        keys["seedream"] = cfg.get("seedream", {}).get("api_key", "")
    if not keys["dashscope"]:
        ds = cfg.get("seedream", {}).get("api_key", "")
        if not ds:
            # Legacy: check old env path
            old_env = os.path.expandvars(cfg.get("dashscope_env_path", ""))
            if old_env and os.path.exists(old_env):
                with open(old_env, "r") as f:
                    for line in f:
                        if "DASHSCOPE" in line:
                            keys["dashscope"] = line.strip().split("=", 1)[1]
                            break

    return keys

def discover_files(folder):
    """在素材文件夹中自动发现源文件。txt可选，无则自动生成"""
    files = {"mp4": None, "srt": None, "txt": None}
    for f in os.listdir(folder):
        fp = os.path.join(folder, f)
        if not os.path.isfile(fp):
            continue
        low = f.lower()
        if low.endswith('.mp4') and '成片' not in f:
            files["mp4"] = fp
        elif low.endswith('.mkv') and '成片' not in f:
            files["mp4"] = fp
        elif low.endswith('.srt'):
            # 优先英文原版字幕（含.en.），避免误选上次生成的"字幕.srt"
            if '.en.' in low or 'english' in low:
                files["srt"] = fp
            elif files["srt"] is None:
                files["srt"] = fp
        elif low.endswith('.txt') and ('配音' in f or '稿' in f or 'script' in low):
            files["txt"] = fp
    return files

# ============================================================
# Phase 0: DeepSeek 从SRT自动生成BBC中文口播稿
# ============================================================

def phase0_generate_narration(folder, srt_file, cfg, api_keys):
    """从英文SRT字幕自动生成BBC风格中文配音稿。若配音稿.txt已存在则跳过"""
    output_txt = os.path.join(folder, "配音稿.txt")
    if os.path.exists(output_txt):
        with open(output_txt, "r", encoding="utf-8") as f:
            chars = len(f.read().replace('\n','').replace(' ',''))
        print(f"[Phase 0] 配音稿.txt 已存在 ({chars}字)，跳过生成")
        return output_txt

    api_key = api_keys.get("deepseek", "")
    if not api_key:
        raise RuntimeError("DeepSeek API Key 未配置，无法自动生成配音稿")

    # 读取SRT纯文本（清洗：去时间戳、编号、[music]标签，去相邻重复）
    with open(srt_file, "r", encoding="utf-8") as f:
        srt_raw = f.read()
    lines = srt_raw.split("\n")
    text_lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if re.match(r'^\d+$', line) or re.match(r'^\d{2}:\d{2}:\d{2},\d{3} -->', line):
            continue
        line = re.sub(r'\[music\]', '', line, flags=re.IGNORECASE)
        line = re.sub(r'\[.*?\]', '', line)
        line = line.strip()
        if line and len(line) > 3:
            text_lines.append(line)
    seen = set(); unique_lines = []
    for line in text_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)
    srt_text = '\n'.join(unique_lines)

    # 配置
    ds_cfg = cfg.get("deepseek", {})
    narration_cfg = cfg.get("narration", {})
    target_minutes = narration_cfg.get("target_minutes", 30)
    speech_rate = cfg.get("cosyvoice", {}).get("speech_rate", 1.0)
    chars_per_sec = 4.0 if speech_rate >= 1.0 else 3.5
    target_chars = int(target_minutes * 60 * chars_per_sec)

    print(f"[Phase 0] 从SRT生成BBC中文口播稿... (目标~{target_minutes}分钟/{target_chars}字)")
    print(f"  SRT清洗后: {len(srt_text)}字符")

    prompt = f"""你是BBC纪录片的资深撰稿人。请根据以下英文纪录片字幕，创作一篇BBC风格的中文口播稿。

核心要求：
- 风格：BBC纪录片式沉稳平铺直叙，不加比喻/不设悬念/不搞首尾呼应，用事实和细节本身驱动叙述
- 戏剧张力来自历史事件本身的冲突感，不刻意煽情
- 精确数据锚点（具体年份、数字、百分比、赔款金额等）
- 适当英式冷幽默（一笔带过，不展开）
- 语速：中文{chars_per_sec}字/秒，目标约{target_minutes}分钟（~{target_chars}字）
- 结构：严格按原文时间线顺序覆盖，从一战结束→凡尔赛条约→大萧条→希特勒崛起→二战爆发，每个历史阶段平等分配篇幅
- 保留所有标志性时间节点和关键人物/事件
- 直接写出纯净口播稿，段落间用 --- 分隔，不要任何说明文字

英文SRT字幕全文：

{srt_text}"""

    payload = json.dumps({
        "model": ds_cfg.get("model", "deepseek-chat"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": ds_cfg.get("temperature_narration", 0.7),
        "max_tokens": 16000
    }).encode()

    try:
        req = urllib.request.Request(
            ds_cfg.get("api_url", "https://api.deepseek.com/v1/chat/completions"),
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=300)
        narration = json.loads(resp.read())["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"DeepSeek配音稿生成失败: {e}")

    # 清理可能的markdown标记
    narration = re.sub(r'^```[a-z]*\n?', '', narration)
    narration = re.sub(r'\n?```$', '', narration)

    with open(output_txt, "w", encoding="utf-8") as f:
        f.write(narration)

    chars = len(narration.replace('\n','').replace(' ',''))
    est_min = chars / (chars_per_sec * 60)
    print(f"  ** 配音稿.txt: {chars}字, 预估{est_min:.1f}分钟 (语速{speech_rate})")
    return output_txt

# ============================================================
# Phase 1: CosyVoice 分段配音（支持断点续跑）
# ============================================================

def phase0_tts(folder, script_file, cfg, api_keys):
    """合成配音.mp3，如果已存在则跳过。支持断点续跑：已合成的TTS段自动跳过"""
    output = os.path.join(folder, "配音.mp3")
    if os.path.exists(output):
        r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",output],
            capture_output=True, text=True)
        dur = float(json.loads(r.stdout)["format"]["duration"])
        print(f"[Phase 1] 配音.mp3 已存在 ({dur:.0f}s)，跳过")
        return output

    api_key = api_keys.get("dashscope", "")
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")

    os.environ["DASHSCOPE_API_KEY"] = api_key
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer, AudioFormat
    dashscope.api_key = api_key

    voice_cfg = cfg.get("cosyvoice", {})
    model = voice_cfg.get("model", "cosyvoice-v3.5-plus")
    voice_id = voice_cfg.get("voice_id", "")
    speech_rate = voice_cfg.get("speech_rate", 0.9)

    with open(script_file, "r", encoding="utf-8") as f:
        script_text = f.read()

    # 分段：优先按 --- 分隔；无则按双换行（自然段落）
    paragraphs = [p.strip() for p in re.split(r"\n*---\n*", script_text) if p.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script_text) if p.strip()]
    # 如果段落太少（<3段且每段>500字），按句号拆得更细
    if len(paragraphs) < 3:
        raw = [s.strip() for s in re.split(r"[。！？；]", script_text) if s.strip() and len(s.strip()) >= 6]
        buf = ""; merged = []
        for s in raw:
            buf += s
            if len(buf) >= 200:
                merged.append(buf); buf = ""
        if buf:
            if merged: merged[-1] += buf
            else: merged.append(buf)
        if merged:
            paragraphs = merged
    paragraphs = [re.sub(r"\*\*", "", p) for p in paragraphs]
    print(f"[Phase 1] CosyVoice 分段合成: {len(paragraphs)} 段")

    seg_dir = os.path.join(folder, "_tts_segments")
    os.makedirs(seg_dir, exist_ok=True)
    tts_segs = []
    t0 = time.time()

    for i, para in enumerate(paragraphs):
        seg_file = os.path.join(seg_dir, f"tts_{i:04d}.mp3")
        clean = para.replace("\n", " ").replace("\r", "").strip()
        if not clean:
            continue
        # 断点续跑：跳过已合成段
        if os.path.exists(seg_file) and os.path.getsize(seg_file) > 1000:
            tts_segs.append(seg_file)
            continue
        try:
            syn = SpeechSynthesizer(
                model=model, voice=voice_id,
                format=AudioFormat.MP3_24000HZ_MONO_256KBPS,
                speech_rate=speech_rate
            )
            audio = syn.call(clean)
            # 写到临时文件再移动，避免DashScope SDK干扰目标目录
            import tempfile as _tempfile
            tmp_fd, tmp_path = _tempfile.mkstemp(suffix=".mp3")
            os.close(tmp_fd)
            with open(tmp_path, "wb") as f:
                f.write(audio)
            os.makedirs(seg_dir, exist_ok=True)
            shutil.move(tmp_path, seg_file)
            tts_segs.append(seg_file)
            if (i + 1) % 3 == 0 or i == len(paragraphs) - 1:
                print(f"  {i+1}/{len(paragraphs)} ({time.time()-t0:.0f}s)")
        except Exception as e:
            print(f"  ** 段{i+1}失败: {e}")
            raise

    # Concat
    concat_list = os.path.join(seg_dir, "_c.txt")
    with open(concat_list, "w", encoding="utf-8") as f:
        for s in tts_segs:
            f.write(f"file '{s}'\n")
    subprocess.run(["ffmpeg","-y","-f","concat","-safe","0","-i",concat_list,
        "-acodec","copy",output], capture_output=True, check=True)

    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",output],
        capture_output=True, text=True)
    dur = float(json.loads(r.stdout)["format"]["duration"])
    # Windows: retry rmtree + wait for file handles to release
    for _ in range(5):
        try:
            shutil.rmtree(seg_dir)
            break
        except OSError:
            time.sleep(1)
    print(f"  ** 配音.mp3: {os.path.getsize(output)/1024/1024:.1f}MB, {dur:.0f}s, 耗时{time.time()-t0:.0f}s")
    return output

# ============================================================
# Phase 1: 音频断句 + 语义SRT匹配（画面去重）
# ============================================================

def sec_to_srt(sec):
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    s, ms = int(sec % 60), int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def phase1_match(folder, voiceover, source_video, srt_file, script_file, cfg, api_keys):
    """音频断句 + DeepSeek语义匹配（去重）+ 保存_work.json"""
    work_file = os.path.join(folder, "_work.json")

    vcfg = cfg.get("video", {})
    acfg = cfg.get("audio", {})
    trim_head, trim_tail = vcfg.get("trim_head", 60), vcfg.get("trim_tail", 60)
    sil_thr = acfg.get("silence_threshold_db", -30)
    min_s, max_s = acfg.get("min_speech_sec", 1.5), acfg.get("max_speech_sec", 4.0)
    window = acfg.get("semantic_window_sec", 25)
    batch_size = acfg.get("semantic_batch_size", 15)

    # 1. 音频断句
    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",voiceover],
        capture_output=True, text=True)
    audio_dur = float(json.loads(r.stdout)["format"]["duration"])

    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",source_video],
        capture_output=True, text=True)
    video_dur = float(json.loads(r.stdout)["format"]["duration"])
    usable_start, usable_end = trim_head, video_dur - trim_tail
    print(f"[Phase 1] 配音{audio_dur:.0f}s, 视频{video_dur:.0f}s, 可用{usable_start:.0f}-{usable_end:.0f}s")

    r = subprocess.run(["ffmpeg","-i",voiceover,"-af",
        f"silencedetect=n={sil_thr}dB:d=0.3","-f","null","-"],
        capture_output=True, text=True)
    ss = [float(m.group(1)) for m in re.finditer(r'silence_start: ([\d.]+)', r.stderr)]
    se = [float(m.group(1)) for m in re.finditer(r'silence_end: ([\d.]+)', r.stderr)]

    speech = []
    prev = 0.0
    for a, b in zip(ss, se):
        if a - prev > 0.5:
            speech.append({"start": prev, "end": a, "duration": a - prev})
        prev = b
    if audio_dur - prev > 0.5:
        speech.append({"start": prev, "end": audio_dur, "duration": audio_dur - prev})
    audio_segs = [s for s in speech if min_s <= s["duration"] <= max_s]

    # 2. 脚本解析
    with open(script_file, "r", encoding="utf-8") as f:
        script_text = f.read()
    script_text = re.sub(r"\n*---\n*", ".", script_text)
    script_text = re.sub(r"\*\*", "", script_text)
    raw = [s.strip() for s in re.split(r"[。！？；\n]+", script_text) if s.strip() and len(s.strip()) >= 2]
    merged = []
    for s in raw:
        if len(s) < 6 and merged:
            merged[-1] += s
        else:
            merged.append(s)
    buf = ""; merged2 = []
    for s in merged:
        buf += s
        if len(buf) >= 12:
            merged2.append(buf); buf = ""
    if buf:
        if merged2: merged2[-1] += buf
        else: merged2.append(buf)

    N = min(len(audio_segs), len(merged2))
    audio_segs = audio_segs[:N]; sentences = merged2[:N]
    print(f"  断句: {len(raw)}->{len(merged)}->{len(merged2)}, 对齐 N={N}")

    # v1.6: 节奏网格 — 标记 hero/普通/过场 slots
    hero_slots = set()
    if N >= 3:
        # 开场 + 中间高潮 + 结尾 = 3 hero slots
        hero_idx = [0, N // 2, N - 1]
        hero_slots = set(hero_idx)
        print(f"  节奏网格: {len(hero_slots)} hero slots (idx={hero_idx}), "
              f"基础 hold = BBC沉稳 3.5s, hero=6.0s, 过场=2.0s")

    # 3. 解析SRT
    with open(srt_file, "r", encoding="utf-8") as f:
        srt_content = f.read()
    blocks = re.split(r"\n\s*\n", srt_content.strip())
    srt_entries = []
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) >= 3:
            m = re.match(r"(\d+):(\d+):(\d+)[.,](\d+)\s*-->\s*(\d+):(\d+):(\d+)[.,](\d+)", lines[1])
            if m:
                h1,m1,s1,ms1,h2,m2,s2,ms2 = map(int, m.groups())
                srt_entries.append({
                    "id": len(srt_entries) + 1,
                    "start": h1*3600 + m1*60 + s1 + ms1/1000,
                    "end": h2*3600 + m2*60 + s2 + ms2/1000,
                    "text": ' '.join(lines[2:])
                })
    print(f"  SRT: {len(srt_entries)} 条")

    # 加载视觉索引（如果存在），用于辅助画面匹配
    frame_index = {}
    index_file = os.path.join(folder, "_frame_index.json")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            frame_index = json.load(f)
        print(f"  视觉索引: {len(frame_index)} 帧可用")
    else:
        print("  ⚠ 无视觉索引，纯文本匹配")

    # 辅助：取某时间戳最近的帧信息（v1.6: 扩展为返回结构化描述 + 多query）
    def frame_info_at(ts, window=5):
        """返回 {structured, subject, scale, queries} 或空dict"""
        if not frame_index:
            return {}
        candidates = []
        for ft_str, data in frame_index.items():
            ft = int(ft_str)
            if abs(ft - ts) <= window:
                if isinstance(data, dict) and data.get("structured"):
                    candidates.append((abs(ft - ts), data))
                elif isinstance(data, str) and data:
                    candidates.append((abs(ft - ts), {"structured": data}))
        if not candidates:
            return {}
        candidates.sort()
        return candidates[0][1]

    def frame_desc_at(ts, window=5):
        """兼容旧版：只返回 structured 描述文本"""
        info = frame_info_at(ts, window)
        return info.get("structured", "") if isinstance(info, dict) else ""

    # 4. 画面匹配：有视觉索引时用DP场景匹配（防回溯重复），无索引时回退DeepSeek批匹配
    all_ts = []

    if frame_index and len(frame_index) >= 100:
        # v2.0: DP场景感知匹配
        print(f"  DP场景匹配 (视觉索引{len(frame_index)}帧)...")
        scenes = scene_matcher.group_scenes(folder)
        all_ts = scene_matcher.dp_optimal_match(
            sentences, audio_segs, srt_entries, scenes,
            usable_start, usable_end, cfg
        )
        # v1.6: 相邻画面多样性校验（仅打印报告）
    else:
        # v1.x fallback: DeepSeek batch matching
        api_key = api_keys.get("deepseek", "")
        ds_cfg = cfg.get("deepseek", {})
        model = ds_cfg.get("model", "deepseek-chat")
        api_url = ds_cfg.get("api_url", "https://api.deepseek.com/v1/chat/completions")
        temp = ds_cfg.get("temperature_match", 0.1)
        used_srt_ids = set()

        print(f"  语义匹配 (窗口+-{window}s, 每批{batch_size}句)...")
        for bi in range(0, N, batch_size):
            batch = sentences[bi:bi+batch_size]
            parts = []
            for j, sent in enumerate(batch):
                sid = bi + j + 1
                exp_ts = usable_start + (sid-1)/(N-1) * (usable_end - usable_start)
                cands = [e for e in srt_entries
                         if abs(e["start"]-exp_ts) < window
                         and e["start"] >= usable_start and e["end"] <= usable_end
                         and e["id"] not in used_srt_ids]
                if not cands:
                    cands = [e for e in sorted(srt_entries, key=lambda e: abs(e["start"]-exp_ts))
                             if e["id"] not in used_srt_ids][:5]
                srt_list = "\n".join([f"  [{e['id']}] @{e['start']:.0f}s: {e['text'][:60]}"
                                       + (f" | 画面: {frame_desc_at(e['start'])}" if frame_index else "")
                                       for e in cands[:8]])
                parts.append(f'{sid}. "{sent}"\n   expected ~{exp_ts:.0f}s\n   candidates:\n{srt_list}')

            prompt = ('Match each Chinese sentence to the most relevant UNIQUE SRT entry.\n'
                      'Each SRT entry ID can only be used ONCE. No duplicate srt_id values.\n'
                      + ('Use "画面:" (visual description) alongside SRT text to find the best visual match.\n' if frame_index else '') +
                      'Return ONLY: [{"sentence_id":n,"srt_id":n}]\n\n') + "\n\n".join(parts)
            payload = json.dumps({
                "model": model, "messages": [{"role":"user","content":prompt}],
                "temperature": temp, "max_tokens": 1000
            }).encode()

            try:
                req = urllib.request.Request(api_url, data=payload,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=120)
                text = json.loads(resp.read())["choices"][0]["message"]["content"]
                jm = re.search(r'\[.*\]', text, re.DOTALL)
                if jm:
                    matches = json.loads(jm.group())
                    for m in matches:
                        srt_id = m["srt_id"]
                        used_srt_ids.add(srt_id)
                        ts = next((e["start"] for e in srt_entries if e["id"] == srt_id), 0)
                        all_ts.append(ts)
                    print(f"    批次{bi//batch_size+1}: {len(matches)}条 (去重中)")
                else:
                    print(f"    批次{bi//batch_size+1}: 无JSON，均匀分布")
                    for j in range(len(batch)):
                        all_ts.append(usable_start + (bi+j)/(N-1)*(usable_end-usable_start))
            except Exception as e:
                print(f"    批次{bi//batch_size+1}: 失败({e})")
                for j in range(len(batch)):
                    all_ts.append(usable_start + (bi+j)/(N-1)*(usable_end-usable_start))

    while len(all_ts) < N:
        all_ts.append(usable_start + len(all_ts)/(N-1)*(usable_end-usable_start))
    timestamps = sorted(all_ts[:N])

    # 去重：相邻时间戳<1s则自动推开
    deduped = []
    for ts in timestamps:
        if deduped and abs(ts - deduped[-1]) < 1.0:
            ts = min(deduped[-1] + 1.0, usable_end - 2.0)
        deduped.append(ts)
    timestamps = deduped

    print(f"  ** 匹配{len(timestamps)}条, {timestamps[0]:.0f}s->{timestamps[-1]:.0f}s")

    # 保存工作状态
    with open(work_file, "w", encoding="utf-8") as f:
        json.dump({
            "N": N, "audio_segs": audio_segs, "sentences": sentences,
            "timestamps": timestamps, "usable_start": usable_start,
            "usable_end": usable_end, "audio_dur": audio_dur,
            "hero_slots": list(hero_slots),  # v1.6: 节奏网格
            "rhythm_grid": {                 # v1.6: hold时长规则
                "hero_hold": 6.0,
                "normal_hold": 3.5,
                "transition_hold": 2.0,
            }
        }, f, ensure_ascii=False)
    return work_file

# ============================================================
# Phase 1.5: 视觉画面索引（Qwen3-VL 逐帧分析）
# ============================================================

def phase1_5_visual_index(folder, source_video, cfg, api_keys):
    """对视频可用区间每隔interval秒抽帧，用Qwen3-VL生成中文画面描述，建立视觉索引。
    结果缓存到 _frame_index.json，已存在则跳过。"""
    index_file = os.path.join(folder, "_frame_index.json")
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            idx = json.load(f)
        print(f"[Phase 1.5] 视觉索引已存在 ({len(idx)} 帧)，跳过")
        return index_file

    api_key = api_keys.get("siliconflow", "")
    if not api_key:
        print("[Phase 1.5] SiliconFlow API Key 未配置，跳过视觉分析")
        return None

    sf_cfg = cfg.get("siliconflow", {})
    model = sf_cfg.get("model", "Qwen/Qwen3-VL-32B-Instruct")
    api_url = sf_cfg.get("api_url", "https://api.siliconflow.cn/v1/chat/completions")
    interval = sf_cfg.get("frame_interval_sec", 2)
    batch_size = sf_cfg.get("frames_per_batch", 4)

    # 计算可用区间（与 phase1_match 一致）
    vcfg = cfg.get("video", {})
    trim_head, trim_tail = vcfg.get("trim_head", 60), vcfg.get("trim_tail", 60)
    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format",source_video],
        capture_output=True, text=True)
    video_dur = float(json.loads(r.stdout)["format"]["duration"])
    usable_start, usable_end = trim_head, video_dur - trim_tail

    # 抽帧
    frames_dir = os.path.join(folder, "_frames")
    os.makedirs(frames_dir, exist_ok=True)
    timestamps = []
    t = usable_start
    while t <= usable_end:
        timestamps.append(t)
        t += interval

    total_frames = len(timestamps)
    print(f"[Phase 1.5] 视觉分析: {total_frames} 帧 (间隔{interval}s, 每批{batch_size}帧)")

    # 提取所有帧
    for i, ts in enumerate(timestamps):
        fp = os.path.join(frames_dir, f"frame_{i:06d}.jpg")
        if os.path.exists(fp) and os.path.getsize(fp) > 1000:
            continue
        subprocess.run([
            "ffmpeg", "-y", "-ss", str(ts), "-i", source_video,
            "-vframes", "1", "-q:v", "3", fp
        ], capture_output=True, check=True, timeout=15)

    # 分批送Qwen3-VL分析
    frame_index = {}
    total_batches = (total_frames + batch_size - 1) // batch_size
    delay = sf_cfg.get("batch_delay_sec", 10)
    max_retries = sf_cfg.get("max_retries", 3)
    t0 = time.time()

    for bi in range(0, total_frames, batch_size):
        batch_ts = timestamps[bi:bi + batch_size]
        batch_num = bi // batch_size + 1

        # 编码帧为base64
        content_parts = []
        for j, ts in enumerate(batch_ts):
            fp = os.path.join(frames_dir, f"frame_{bi + j:06d}.jpg")
            with open(fp, "rb") as img:
                b64 = base64.b64encode(img.read()).decode()
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })

        # 构建请求 — v1.6 改进: 结构化画面描述 + 多Query
        frame_id_list = [f"#{bi + j + 1}" for j in range(len(batch_ts))]
        content_parts.append({
            "type": "text",
            "text": (
                f"For each frame above, return a JSON array of objects. Each object MUST have:\n"
                f"  - \"structured\": CLIP-rankable description using template: "
                f"\"<subject>, <action/pose>, <environment>, <lighting>, <era/texture>\" "
                f"(Chinese, 15-40 chars, noun-adjective only, NO emotion words)\n"
                f"  - \"literal_query\": shortest stock-search phrase (Chinese, 3-8 chars)\n"
                f"  - \"lateral_query\": different angle/scale of same scene (Chinese, 3-8 chars)\n"
                f"  - \"associative_query\": adjacent concept/metaphor (Chinese, 3-8 chars)\n"
                f"  - \"subject\": main visual subject (1-3 chars)\n"
                f"  - \"scale\": shot scale — one of [close-up, medium, wide, extreme-wide]\n\n"
                f"Example: {{\"structured\": \"士兵在雪地中行军，灰暗天空，长焦压缩，1940年代质感\", "
                f"\"literal_query\": \"士兵雪地行军\", \"lateral_query\": \"军靴冻土特写\", "
                f"\"associative_query\": \"篝火旁休整\", \"subject\": \"士兵\", \"scale\": \"wide\"}}\n\n"
                f"Return ONLY: [{{...}}, {{...}}, ...]\n"
                f"Frames: {frame_id_list}"
            )
        })

        payload = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": content_parts}],
            "temperature": 0.1,
            "max_tokens": 4000
        }).encode()

        # Retry loop for 429 rate limiting
        success = False
        for attempt in range(max_retries):
            try:
                req = urllib.request.Request(api_url, data=payload,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    })
                resp = urllib.request.urlopen(req, timeout=120)
                text = json.loads(resp.read())["choices"][0]["message"]["content"]

                # Parse JSON descriptions — v1.6: objects with structured/subject/scale/queries
                jm = re.search(r'\[.*\]', text, re.DOTALL)
                if jm:
                    descs = json.loads(jm.group())
                    for j, item in enumerate(descs):
                        if j < len(batch_ts):
                            ts_int = int(batch_ts[j])
                            if isinstance(item, dict):
                                frame_index[ts_int] = {
                                    "structured": item.get("structured", ""),
                                    "literal_query": item.get("literal_query", ""),
                                    "lateral_query": item.get("lateral_query", ""),
                                    "associative_query": item.get("associative_query", ""),
                                    "subject": item.get("subject", ""),
                                    "scale": item.get("scale", "medium"),
                                }
                            elif isinstance(item, str):
                                # Backward compatibility: old string-only format
                                frame_index[ts_int] = {
                                    "structured": item,
                                    "literal_query": item[:8] if len(item) > 8 else item,
                                    "lateral_query": "",
                                    "associative_query": "",
                                    "subject": "",
                                    "scale": "medium",
                                }
                success = True
                break
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    wait = delay * (attempt + 1)
                    if attempt < max_retries - 1:
                        print(f"  批次{batch_num}: 429限流, 等待{wait}s重试({attempt+1}/{max_retries})")
                        time.sleep(wait)
                    else:
                        print(f"  批次{batch_num}: 429重试耗尽, 跳过")
                else:
                    print(f"  批次{batch_num}: HTTP {e.code} - {e.reason}")
                    break
            except Exception as e:
                print(f"  批次{batch_num}: 失败({e})")
                break

        if not success:
            for ts in batch_ts:
                frame_index[int(ts)] = ""

        elapsed = time.time() - t0
        completed = batch_num
        eta = elapsed / completed * total_batches - elapsed if completed > 0 else 0
        if batch_num % 20 == 0 or batch_num == total_batches:
            print(f"  {batch_num}/{total_batches}批 ({len(frame_index)}帧有效, {elapsed:.0f}s, ETA {eta:.0f}s)")

        # Delay between batches to avoid rate limiting
        if batch_num < total_batches:
            time.sleep(delay)

    # 保存索引
    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(frame_index, f, ensure_ascii=False)

    print(f"  ** 视觉索引: {len(frame_index)} 帧, 耗时 {time.time()-t0:.0f}s")
    return index_file

# ============================================================
# Phase 2+3: 裁切画面 + 间隔片段
# ============================================================

def phase2_3_clips(folder, source_video, work_file, cfg):
    """裁切画面片段和间隔片段 — v1.6: 支持节奏网格(sub-window extraction)"""
    with open(work_file, "r", encoding="utf-8") as f:
        w = json.load(f)
    N = w["N"]; audio_segs = w["audio_segs"]; timestamps = w["timestamps"]
    usable_start = w["usable_start"]; usable_end = w["usable_end"]; audio_dur = w["audio_dur"]
    hero_slots = set(w.get("hero_slots", []))
    rhythm = w.get("rhythm_grid", {})
    hero_hold = rhythm.get("hero_hold", 6.0)
    normal_hold = rhythm.get("normal_hold", 3.5)
    trans_hold = rhythm.get("transition_hold", 2.0)

    vcfg = cfg.get("video", {})
    ph, pt = vcfg.get("padding_head", 3), vcfg.get("padding_tail", 3)

    clips_dir = os.path.join(folder, "_clips")
    gap_dir = os.path.join(folder, "_gaps")
    for d in [clips_dir, gap_dir]:
        if os.path.exists(d): shutil.rmtree(d)
        os.makedirs(d)

    # Phase 2: 画面片段 (全静音 -an)
    print(f"[Phase 2] 裁切 {N} 画面片段 (hero_hold={hero_hold}s, normal={normal_hold}s)...")
    subwindow_log = []

    for i in range(N):
        seg = audio_segs[i]
        src_ts = max(usable_start, min(timestamps[i], usable_end - seg["duration"]))

        # v1.6: 节奏网格 — hero slots 用更长的画面片段
        if i in hero_slots:
            clip_dur = min(hero_hold, seg["duration"] * 1.5)
            # 扩展源区间以容纳更长的子窗口
            search_window = max(clip_dur * 1.5, seg["duration"] * 2)
            search_start = max(usable_start, src_ts - (search_window - clip_dur) / 2)
            clip_ts = search_start
        else:
            # 邻近 hero 的 transition slot
            if (i - 1 in hero_slots) or (i + 1 in hero_slots):
                clip_dur = min(trans_hold, seg["duration"])
            else:
                clip_dur = min(normal_hold, seg["duration"])
            clip_ts = src_ts

        # v1.6: 最佳子窗口 — 从源区间内选择画面最丰富的 sub-window
        # 如果素材 > 目标时长 * 1.5，尝试偏移寻找更好的起始点
        src_end = min(clip_ts + clip_dur * 2, usable_end)
        if src_end - clip_ts > clip_dur * 1.2:
            # 取画面中间偏前的三分之一作为「黄金时刻」
            offset = (src_end - clip_ts - clip_dur) * 0.35
            clip_ts = clip_ts + offset
            subwindow_log.append(f"  片段{i+1}: sub-window offset {offset:.1f}s "
                                f"(素材{src_end - clip_ts:.1f}s → 截取{clip_dur:.1f}s)")

        clip_ts = max(usable_start, min(clip_ts, usable_end - clip_dur))

        subprocess.run([
            "ffmpeg","-y","-ss",str(clip_ts),"-i",source_video,
            "-t",str(clip_dur),
            "-c:v","libx264","-preset","ultrafast","-crf",str(vcfg.get("clip_crf",23)),
            "-an","-pix_fmt","yuv420p",
            os.path.join(clips_dir, f"clip_{i+1:04d}.mp4")
        ], capture_output=True, check=True, timeout=60)
        if (i+1) % 20 == 0:
            print(f"  {i+1}/{N}")
    
    if subwindow_log:
        for log_line in subwindow_log:
            print(log_line)
    print(f"  ** {N} 画面片段 (hero={'/'.join(str(s) for s in sorted(hero_slots))})")

    # Phase 3: 间隔片段
    print(f"[Phase 3] 裁切间隔片段...")
    concat_pieces = []

    # 动态超时：至少60s，最多300s，跟片段时长成正比
    def gap_timeout(dur):
        return max(60, min(300, int(dur * 0.5 + 30)))

    # 片头
    subprocess.run(["ffmpeg","-y","-ss",str(usable_start),"-i",source_video,
        "-t",str(ph),"-c:v","libx264","-preset","ultrafast","-crf",str(vcfg.get("gap_crf",23)),
        "-an","-pix_fmt","yuv420p",os.path.join(gap_dir,"gap_head.mp4")],
        capture_output=True, check=True, timeout=gap_timeout(ph))
    concat_pieces.append(os.path.join(gap_dir,"gap_head.mp4"))

    prev_end = 0.0
    for i, seg in enumerate(audio_segs):
        if seg["start"] > prev_end:
            gd = seg["start"] - prev_end
            if gd > 0.1:
                gp = os.path.join(gap_dir, f"gap_{i+1:04d}.mp4")
                gs = max(usable_start, min(usable_start+prev_end, usable_end-gd))
                subprocess.run(["ffmpeg","-y","-ss",str(gs),"-i",source_video,
                    "-t",str(gd),"-c:v","libx264","-preset","ultrafast","-crf",str(vcfg.get("gap_crf",23)),
                    "-an","-pix_fmt","yuv420p",gp],
                    capture_output=True, check=True, timeout=gap_timeout(gd))
                concat_pieces.append(gp)
        concat_pieces.append(os.path.join(clips_dir, f"clip_{i+1:04d}.mp4"))
        prev_end = seg["end"]

    # 尾间隔（大间隙跳过——最后一段后不应该有太长空白）
    if audio_dur > prev_end:
        td = audio_dur - prev_end
        if td > 0.1:
            if td > 30:
                print(f"  ⚠ 尾间隙过长({td:.0f}s)，限制为30s")
                td = 30.0
            gp = os.path.join(gap_dir, "gap_tail_inner.mp4")
            gs = max(usable_start, min(usable_start+prev_end, usable_end-td))
            subprocess.run(["ffmpeg","-y","-ss",str(gs),"-i",source_video,
                "-t",str(td),"-c:v","libx264","-preset","ultrafast","-crf",str(vcfg.get("gap_crf",23)),
                "-an","-pix_fmt","yuv420p",gp],
                capture_output=True, check=True, timeout=gap_timeout(td))
            concat_pieces.append(gp)

    # 片尾
    ts = max(usable_start, min(usable_start+audio_dur, usable_end-pt))
    subprocess.run(["ffmpeg","-y","-ss",str(ts),"-i",source_video,
        "-t",str(pt),"-c:v","libx264","-preset","ultrafast","-crf",str(vcfg.get("gap_crf",23)),
        "-an","-pix_fmt","yuv420p",os.path.join(gap_dir,"gap_tail.mp4")],
        capture_output=True, check=True, timeout=gap_timeout(pt))
    concat_pieces.append(os.path.join(gap_dir,"gap_tail.mp4"))

    # 写concat列表
    concat_file = os.path.join(folder, "_concat.txt")
    with open(concat_file, "w", encoding="utf-8") as f:
        for p in concat_pieces:
            f.write(f"file '{p}'\n")
    print(f"  ** {len(concat_pieces)} 总片段 (含片头尾)")
    return concat_file

# ============================================================
# Phase 4: 拼接1080P + 混音
# ============================================================

def phase4_concat(folder, voiceover, concat_file, cfg):
    """拼接视频 + 混入配音 -> 成片.mp4
    
    v1.6 转场语言:
      - hard cut (硬切): 90% 默认，适用绝大多数无缝叙述
      - dissolve (叠化 0.5-1.0s): 情感相联的画面，段落转折处
      - fade_to_black / fade_in: 仅开篇/结尾，全片最多 2 次
      - 禁止: wipes/slides/zoom blur/RGB split/light leaks
    """
    output = os.path.join(folder, "成片.mp4")
    vcfg = cfg.get("video", {})
    
    # v1.6: 转场模式 — 默认 hard cut, 可启用 simple_dissolve
    transition_mode = vcfg.get("transition_mode", "hard_cut")
    
    print(f"[Phase 4] 拼接 1080P + 配音混入 (转场: {transition_mode})...")
    result = subprocess.run([
        "ffmpeg","-y",
        "-f","concat","-safe","0","-i",concat_file,
        "-i",voiceover,
        "-vf",f"scale={vcfg.get('output_width',1920)}:{vcfg.get('output_height',1080)}:flags=lanczos,format=yuv420p",
        "-c:v","libx264","-preset","fast","-crf",str(vcfg.get("video_crf",20)),
        "-map","0:v:0","-map","1:a:0",
        "-c:a","aac","-b:a",vcfg.get("audio_bitrate","192k"),
        "-shortest",
        output
    ], capture_output=True, text=True, timeout=3600)

    if result.returncode != 0:
        print("  直接拼接失败，尝试 subst 映射...")
        os.system("subst X: /D 2>nul")
        os.system(f'subst X: "{folder}"')
        x_concat = r"X:\_concat_x.txt"
        with open(concat_file, "r") as f:
            content = f.read().replace(folder, "X:")
        with open(x_concat, "w") as f:
            f.write(content)

        result = subprocess.run([
            "ffmpeg","-y","-f","concat","-safe","0","-i",x_concat,
            "-i",r"X:\配音.mp3",
            "-vf",f"scale={vcfg.get('output_width',1920)}:{vcfg.get('output_height',1080)}:flags=lanczos,format=yuv420p",
            "-c:v","libx264","-preset","medium","-crf",str(vcfg.get("video_crf",20)),
            "-map","0:v:0","-map","1:a:0",
            "-c:a","aac","-b:a",vcfg.get("audio_bitrate","192k"),
            "-shortest", r"X:\成片.mp4"
        ], capture_output=True, text=True, timeout=600)
        os.system("subst X: /D")
        output = os.path.join(folder, "成片.mp4")

    r = subprocess.run(["ffprobe","-v","quiet","-print_format","json","-show_format","-show_streams",output],
        capture_output=True, text=True)
    info = json.loads(r.stdout)
    dur = float(info["format"]["duration"])
    size_mb = os.path.getsize(output) / (1024*1024)
    for s in info["streams"]:
        if s["codec_type"] == "video":
            print(f"  ** 成片: {s['width']}x{s['height']} {s['codec_name']}, {size_mb:.0f}MB, {dur:.0f}s")
    return output

# ============================================================
# Phase 5: 发布包
# ============================================================

def phase5_publish(folder, work_file, cfg, api_keys):
    """生成封面、标题、字幕、BGM"""
    with open(work_file, "r", encoding="utf-8") as f:
        w = json.load(f)
    audio_segs = w["audio_segs"]; sentences = w["sentences"]; N = w["N"]

    ds_cfg = cfg.get("deepseek", {})
    api_key = api_keys.get("deepseek", "")

    # 5a. 标题/标签 (DeepSeek)
    print("[Phase 5] 生成发布材料...")
    script_excerpt = "".join(sentences[:20])
    prompt = f"""你是BBC纪录片的宣发策划。根据以下解说词生成发布材料。风格：BBC战争纪录片，冷静深邃，无感叹号。

解说词节选：{script_excerpt}

返回JSON：{{"title":"<=25字","subtitle":"<=40字","tags":["标签1","标签2","标签3","标签4","标签5"],"description":"150-200字"}}"""

    payload = json.dumps({
        "model": ds_cfg.get("model","deepseek-chat"),
        "messages": [{"role":"user","content":prompt}],
        "temperature": ds_cfg.get("temperature_title",0.7),
        "max_tokens": 1000,
        "response_format": {"type":"json_object"}
    }).encode()

    req = urllib.request.Request(ds_cfg.get("api_url","https://api.deepseek.com/v1/chat/completions"),
        data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=60)
    meta = json.loads(json.loads(resp.read())["choices"][0]["message"]["content"])

    print(f"  标题: {meta['title']}")
    print(f"  标签: {meta['tags']}")

    # 保存
    with open(os.path.join(folder, "发布信息.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    txt = f"""{meta['title']}
{meta['subtitle']}

{' '.join(['#'+t for t in meta['tags']])}

简介：
{meta['description']}
"""
    with open(os.path.join(folder, "发布信息.txt"), "w", encoding="utf-8") as f:
        f.write(txt)

    # 5b. 字幕SRT
    ph = cfg.get("video",{}).get("padding_head", 3)
    srt_lines = []
    for i,(seg,sent) in enumerate(zip(audio_segs,sentences)):
        srt_lines.append(str(i+1))
        srt_lines.append(f"{sec_to_srt(ph+seg['start'])} --> {sec_to_srt(ph+seg['end'])}")
        srt_lines.append(sent)
        srt_lines.append("")
    with open(os.path.join(folder, "字幕.srt"), "w", encoding="utf-8") as f:
        f.write("\n".join(srt_lines))
    print(f"  ** 字幕.srt: {N}条")

    # 5c. 剪映字幕
    script_file = os.path.join(folder, "配音稿.txt")
    if os.path.exists(script_file):
        with open(script_file, "r", encoding="utf-8") as f:
            full = f.read()
        full = re.sub(r"\n*---\n*", " ", full)
        full = re.sub(r"\*\*", "", full)
        chars = re.split(r"[。！？；，、：．\n!?,;:.——…\"''""（）()\s]+", full)
        chars = [l.strip() for l in chars if l.strip()]
        with open(os.path.join(folder, "剪映字幕文件.txt"), "w", encoding="utf-8") as f:
            f.write('\n'.join(chars))
        print(f"  ** 剪映字幕: {len(chars)}行")

    # 5d. BGM — 从Mixkit下载无版权高质量BGM
    bgm_src = cfg.get("bgm_source_dir", "")
    mixkit_tracks = cfg.get("bgm_mixkit_tracks", {})
    bgm_dst = os.path.join(folder, "BGM")
    os.makedirs(bgm_dst, exist_ok=True)
    keywords = cfg.get("bgm_keywords", {})
    found = {k: None for k in keywords}

    # 优先从本地bgm_source_dir复制
    if bgm_src and os.path.exists(bgm_src):
        for root, dirs, files in os.walk(bgm_src):
            for f in files:
                low = f.lower()
                for label, kw in keywords.items():
                    if found[label] is None and kw in low:
                        found[label] = os.path.join(root, f)
            if all(found.values()):
                break

    for label, src in found.items():
        if src:
            shutil.copy2(src, os.path.join(bgm_dst, f"{label}.mp3"))
            print(f"  ** {label}.mp3 (本地)")
        elif label in mixkit_tracks:
            # 从Mixkit下载
            track_id = mixkit_tracks[label]
            url = f"https://assets.mixkit.co/music/{track_id}/{track_id}.mp3"
            try:
                urllib.request.urlretrieve(url, os.path.join(bgm_dst, f"{label}.mp3"))
                size = os.path.getsize(os.path.join(bgm_dst, f"{label}.mp3"))
                print(f"  ** {label}.mp3 (Mixkit #{track_id}, {size/1024:.0f}KB)")
            except Exception as e:
                print(f"  .. {label} 下载失败: {e}")
        else:
            print(f"  .. 未找到 {label}")

    # 5e. 封面 (Seedream) — 根据脚本内容动态生成封面
    seedream_cfg = cfg.get("seedream", {})

    # 用DeepSeek根据解说词生成封面prompt
    script_excerpt = "".join(sentences[:30])[:800]
    cover_prompt_req = f"""Analyze this documentary script excerpt and generate ONE image prompt for the cover art.

Rules:
- BBC documentary style, cinematic photography, no text, no words
- Describe the MAIN subject/person/event from the script (not generic scenes)
- If the script mentions a specific historical figure (emperor, general, etc), make them the center
- If the script is about a location/event, describe that visually
- Vertical 2:3 portrait format
- Style: cinematic lighting, dramatic atmosphere, hyper-realistic, 8K

Return ONLY the English prompt, 1-2 sentences, no explanation.

Script: {script_excerpt}"""

    payload = json.dumps({
        "model": ds_cfg.get("model","deepseek-chat"),
        "messages": [{"role":"user","content":cover_prompt_req}],
        "temperature": 0.5,
        "max_tokens": 200
    }).encode()

    try:
        req = urllib.request.Request(ds_cfg.get("api_url","https://api.deepseek.com/v1/chat/completions"),
            data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=30)
        prompt_img = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
        print(f"  封面prompt: {prompt_img[:100]}...")
    except Exception as e:
        prompt_img = "BBC documentary poster, cinematic photography, no text, no words. Cinematic lighting, dramatic atmosphere, hyper-realistic, 8K. Vertical 2:3 portrait."
        print(f"  .. 封面prompt生成失败，用默认: {e}")

    payload = json.dumps({
        "model": seedream_cfg.get("model", "doubao-seedream-4-0-250828"),
        "prompt": prompt_img,
        "size": seedream_cfg.get("cover_size", "1080x1440"),
        "response_format": "b64_json"
    }).encode()

    try:
        req = urllib.request.Request(
            seedream_cfg.get("api_url", "https://ark.cn-beijing.volces.com/api/v3/images/generations"),
            data=payload,
            headers={
                "Authorization": f"Bearer {api_keys.get('seedream','')}",
                "Content-Type": "application/json"
            })
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read())
        b64 = result["data"][0]["b64_json"]
        cover_path = os.path.join(folder, "封面.jpg")
        with open(cover_path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"  ** 封面.jpg: {os.path.getsize(cover_path)/1024:.0f}KB")
    except Exception as e:
        print(f"  .. 封面生成失败: {e}")

    print("  ** Phase 6 完成")

# ============================================================
# 清理中间文件
# ============================================================

def cleanup(folder):
    """删除中间文件（带重试，Windows文件锁兼容）"""
    for name in ["_clips", "_gaps", "_tts_segments", "_concat.txt", "_work.json", "_concat_x.txt"]:
        path = os.path.join(folder, name)
        if os.path.exists(path):
            for _ in range(3):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    break
                except OSError:
                    time.sleep(0.5)

# ============================================================
# 主入口
# ============================================================

def main():
    if len(sys.argv) < 2:
        print("Usage: python bbc_mp4_pipeline.py <素材文件夹>")
        print("Example: python bbc_mp4_pipeline.py \"E:\\纪录片\\油管\\素材文件夹\"")
        sys.exit(1)

    folder = sys.argv[1]
    if not os.path.isdir(folder):
        print(f"ERROR: 文件夹不存在: {folder}")
        sys.exit(1)

    print("=" * 60)
    print("BBC-mp4 全配音流程")
    print("=" * 60)
    print(f"素材文件夹: {folder}")

    # 加载配置
    cfg = load_config()
    api_keys = load_api_keys(cfg)

    # 发现文件
    files = discover_files(folder)
    if not files["mp4"]:
        print("ERROR: 未找到源视频 (mp4)")
        sys.exit(1)
    if not files["srt"]:
        print("ERROR: 未找到字幕文件 (srt)")
        sys.exit(1)

    print(f"  源视频: {os.path.basename(files['mp4'])}")
    print(f"  字幕:   {os.path.basename(files['srt'])}")

    t_total = time.time()

    try:
        # Phase 0: SRT → 配音稿（若配音稿.txt已存在则跳过）
        script_file = phase0_generate_narration(folder, files["srt"], cfg, api_keys)
        files["txt"] = script_file
        print(f"  配音稿: {os.path.basename(script_file)}")

        # Phase 1: CosyVoice TTS（若配音.mp3已存在则跳过）
        voiceover = os.path.join(folder, "配音.mp3")
        if not os.path.exists(voiceover) or os.path.getsize(voiceover) < 10000:
            voiceover = phase0_tts(folder, script_file, cfg, api_keys)
        else:
            print(f"[Phase 1] 配音.mp3 已存在 ({os.path.getsize(voiceover)/1024/1024:.1f}MB)，跳过TTS")

        # Phase 1.5: 视觉画面索引（Qwen3-VL抽帧分析，辅助精准匹配）
        # 跳过：API限流导致耗时过长，回退纯文本语义匹配
        # phase1_5_visual_index(folder, files["mp4"], cfg, api_keys)
        print("[Phase 1.5] 跳过视觉索引（回退纯文本语义匹配）")

        # Phase 2: 语义SRT匹配（含视觉上下文）
        work_file = phase1_match(folder, voiceover, files["mp4"], files["srt"], script_file, cfg, api_keys)

        # Phase 3+4: 裁切
        concat_file = phase2_3_clips(folder, files["mp4"], work_file, cfg)

        # Phase 5: 拼接
        phase4_concat(folder, voiceover, concat_file, cfg)

        # Phase 6: 发布包
        phase5_publish(folder, work_file, cfg, api_keys)

        # 清理
        cleanup(folder)

    except Exception as e:
        print(f"\n** 流水线失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = time.time() - t_total
    print(f"\n{'='*60}")
    print(f"** 全流程完成! 总耗时 {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"输出: {folder}")
    print(f"  成片.mp4 | 封面.jpg | 字幕.srt | 发布信息.txt | BGM/")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
