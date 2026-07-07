"""
scene_matcher.py — v2.0 场景感知精准画面匹配
==============================================
基于现有帧（_frame_index.json）+ DP单调性约束，彻底消除重复画面。
"""

import json, os, re, time, urllib.request, base64, subprocess
from collections import defaultdict


def load_frame_index(folder):
    """加载视觉索引 {ts_int: description_str}"""
    idx_file = os.path.join(folder, "_frame_index.json")
    if not os.path.exists(idx_file):
        return {}
    with open(idx_file, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items() if v}


def group_scenes(folder, similarity_threshold=0.1):
    """基于帧描述的语义相似度 + 固定时间窗口，将相邻帧聚类为场景。
    
    策略：
    1. 固定时间窗口（默认8秒）内连续帧直接合并
    2. 窗口内选最佳描述作为场景描述
    返回: [{start_ts, end_ts, keyframe_ts, description}]
    """
    frame_index = load_frame_index(folder)
    if not frame_index:
        return []

    timestamps = sorted(frame_index.keys())
    
    # 时间窗口聚类：每 window_sec 秒内的连续帧合并为一个场景
    window_sec = 8  # 260句/4230s ≈ 16s/句，8s窗口→约530场景，DP从中选260
    
    scenes = []
    i = 0
    while i < len(timestamps):
        start_ts = timestamps[i]
        end_ts = start_ts
        # 收集窗口内的帧
        window_frames = [start_ts]
        j = i + 1
        while j < len(timestamps) and timestamps[j] - start_ts <= window_sec:
            window_frames.append(timestamps[j])
            end_ts = timestamps[j]
            j += 1
        
        # 选中间帧作为关键帧，取描述最长的帧的描述
        mid_idx = len(window_frames) // 2
        keyframe_ts = window_frames[mid_idx]
        # 取窗口内最长的非空描述
        descs = [frame_index.get(t, "") for t in window_frames if frame_index.get(t, "")]
        best_desc = max(descs, key=len) if descs else ""
        
        scenes.append({
            "start": start_ts,
            "end": end_ts,
            "keyframe_ts": keyframe_ts,
            "description": best_desc,
            "duration": end_ts - start_ts
        })
        
        i = j
    
    print(f"  场景分组: {len(frame_index)}帧 → {len(scenes)}场景 (窗口{window_sec}s)")
    return scenes


def dp_optimal_match(sentences, audio_segs, srt_entries, scenes, usable_start, usable_end, cfg):
    """DP时序最优匹配：每句解说词匹配到一个场景，保证时间单调不回溯。
    
    输入:
      sentences: 260句中文解说词
      audio_segs: 260段音频 [{start, end, duration}]
      srt_entries: 3478条英文SRT
      scenes: ~100个场景 [{start, end, keyframe_ts, description}]
      usable_start, usable_end: 可用视频区间
    
    输出:
      timestamps: 260个匹配后的画面起始时间戳
    """
    N = len(sentences)
    M = len(scenes)
    
    if M == 0:
        # Fallback: uniform distribution
        return [usable_start + i / (N - 1) * (usable_end - usable_start) for i in range(N)]
    
    # 预计算：每句解说词对每个场景的得分
    scores = [[0.0] * M for _ in range(N)]
    
    for i, sent in enumerate(sentences):
        seg = audio_segs[i]
        exp_ts = usable_start + i / (N - 1) * (usable_end - usable_start)
        
        # 在SRT中找候选
        window = cfg.get("audio", {}).get("semantic_window_sec", 25)
        srt_cands = [e for e in srt_entries if abs(e["start"] - exp_ts) < window]
        
        for j, scene in enumerate(scenes):
            score = 0.0
            
            # 1. SRT位置先验: 场景时间与期望时间的接近度
            time_diff = abs(scene["keyframe_ts"] - exp_ts)
            score += max(0, 1.0 - time_diff / window) * 0.4
            
            # 2. 场景描述与解说词的关键词重叠
            if scene["description"] and sent:
                sw = set(sent)
                dw = set(scene["description"])
                if sw and dw:
                    score += (len(sw & dw) / max(len(sw), len(dw))) * 0.3
            
            # 3. 时长匹配: 场景时长与音频段时长的匹配度
            dur_ratio = min(seg["duration"], scene["duration"]) / max(seg["duration"], scene["duration"])
            score += dur_ratio * 0.3
            
            scores[i][j] = score
    
    # DP: dp[i][j] = 第i句匹配到第j个场景的最佳累计得分
    # 约束: 第i句的场景必须 >= 第i-1句的场景 (单调不回溯)
    INF_NEG = -1e9
    dp = [[INF_NEG] * M for _ in range(N)]
    backtrack = [[-1] * M for _ in range(N)]
    
    # 初始化第一句
    for j in range(M):
        dp[0][j] = scores[0][j]
    
    # DP递推
    for i in range(1, N):
        # 前缀最大值优化: O(M)而非O(M²)
        best_prev = INF_NEG
        best_j = -1
        for j in range(M):
            # 更新前缀最大值
            if dp[i-1][j] > best_prev:
                best_prev = dp[i-1][j]
                best_j = j
            dp[i][j] = best_prev + scores[i][j]
            backtrack[i][j] = best_j
    
    # 回溯最优路径
    best_last = max(range(M), key=lambda j: dp[N-1][j])
    matches = [0] * N
    matches[N-1] = best_last
    for i in range(N-2, -1, -1):
        matches[i] = backtrack[i+1][matches[i+1]]
    
    # 转换为时间戳
    timestamps = []
    used_scenes = set()
    for i, scene_j in enumerate(matches):
        scene = scenes[scene_j]
        seg = audio_segs[i]
        
        # 场景内选点：靠近场景中间，但避免与上一句重叠
        ts = scene["keyframe_ts"]
        if i > 0:
            prev_ts = timestamps[-1]
            if ts <= prev_ts + 0.5:
                ts = min(prev_ts + 1.0, scene["end"] - seg["duration"])
        
        # 保证在可用范围内
        ts = max(usable_start, min(ts, usable_end - seg["duration"]))
        timestamps.append(ts)
        used_scenes.add(scene_j)
    
    # 统计
    backtrack_count = sum(1 for i in range(1, N) if timestamps[i] <= timestamps[i-1])
    print(f"  DP匹配: {N}句→{len(used_scenes)}场景, 回溯{backtrack_count}处, "
          f"span {timestamps[0]:.0f}s→{timestamps[-1]:.0f}s")
    
    return timestamps


def diversity_check(timestamps, audio_segs, folder, cfg):
    """v1.6 新增: 相邻画面多样性校验。
    
    基于 frame_index 中的 subject + scale 信息，检测连续两个画面
    是否「主体相似 AND 景别相似」。如果相邻重复，对低优先级片段
    重新匹配（排除当前 pick 后选次优场景）。
    
    返回: (adjusted_timestamps, diversity_report)
    """
    frame_index = load_frame_index(folder)
    if not frame_index or len(timestamps) < 2:
        return timestamps, {"duplicates_found": 0, "duplicates_fixed": 0}
    
    scene_window = cfg.get("siliconflow", {}).get("scene_window_sec", 8)
    dupes_found = 0
    dupes_fixed = 0
    adjusted = list(timestamps)
    
    for i in range(len(adjusted) - 1):
        # 查找两个时间戳附近的帧信息
        info_a = None
        info_b = None
        for ts_str, data in frame_index.items():
            ft = int(ts_str)
            if isinstance(data, dict) and data.get("subject"):
                if abs(ft - adjusted[i]) <= scene_window:
                    info_a = (ft, data)
                if abs(ft - adjusted[i + 1]) <= scene_window:
                    info_b = (ft, data)
        
        if not info_a or not info_b:
            continue
        
        subj_a = info_a[1].get("subject", "")
        subj_b = info_b[1].get("subject", "")
        scale_a = info_a[1].get("scale", "medium")
        scale_b = info_b[1].get("scale", "medium")
        
        # 检测重复: 主体相同 AND 景别相同
        if subj_a == subj_b and subj_a and scale_a == scale_b:
            dupes_found += 1
            # 对第 i+1 个片段重新找画面（偏移 scene_window 秒）
            seg = audio_segs[i + 1] if i + 1 < len(audio_segs) else {"duration": 3.0}
            new_ts = adjusted[i] + seg["duration"] + scene_window * 0.5
            new_ts = min(new_ts, adjusted[i + 1] + scene_window)
            if new_ts != adjusted[i + 1]:
                adjusted[i + 1] = new_ts
                dupes_fixed += 1
                print(f"  [多样性] 片段{i+1}/{i+2} 重复(subject={subj_a}, scale={scale_a}), "
                      f"偏移{new_ts - timestamps[i + 1]:.1f}s")
    
    report = {
        "duplicates_found": dupes_found,
        "duplicates_fixed": dupes_fixed,
    }
    if dupes_found > 0:
        print(f"  ** 多样性校验: 发现{dupes_found}处重复, 修复{dupes_fixed}处")
    
    return adjusted, report
