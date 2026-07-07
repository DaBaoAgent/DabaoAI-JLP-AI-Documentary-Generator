"""Load GPT-SoVITS once and synthesise all anchored narration sentences."""

import json
import os
import hashlib
import subprocess
import sys
import wave
from pathlib import Path

from backend.media_tools import ffmpeg, ffprobe


def write_wav(path: Path, audio, sample_rate: int) -> None:
    import numpy as np

    data = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
    data = np.squeeze(data)
    if data.ndim == 1:
        channels = 1
    else:
        if data.shape[0] <= 8 and data.shape[0] < data.shape[-1]:
            data = data.T
        channels = data.shape[1]
    if data.dtype.kind == "f":
        data = np.clip(data, -1.0, 1.0)
        data = (data * 32767.0).astype("<i2")
    else:
        data = data.astype("<i2", copy=False)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(int(sample_rate))
        wav.writeframes(data.tobytes())


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def prepare_reference_audio(job: dict) -> str:
    """Keep GPT-SoVITS reference audio inside its required 3-10 second window."""
    reference = Path(job["reference"])
    duration = probe_duration(reference)
    if 3.0 <= duration <= 10.0:
        return str(reference)

    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(
        f"{reference.resolve()}:{reference.stat().st_mtime_ns}:{duration:.3f}".encode("utf-8")
    ).hexdigest()[:12]
    fixed = output_dir / f"reference_gpt_sovits_{digest}.wav"
    if not fixed.exists() or fixed.stat().st_size <= 1000:
        if duration < 3.0:
            cmd = [ffmpeg(), "-y", "-i", str(reference), "-af", "apad", "-t", "3.200",
                   "-ac", "1", "-ar", "32000", str(fixed)]
        else:
            cmd = [ffmpeg(), "-y", "-i", str(reference), "-t", "9.500",
                   "-ac", "1", "-ar", "32000", str(fixed)]
        subprocess.run(cmd, text=True, encoding="utf-8", errors="replace",
                       capture_output=True, check=True, timeout=120)
    print(
        f"GPT-SoVITS reference audio {duration:.2f}s is outside 3-10s; "
        f"using temporary {probe_duration(fixed):.2f}s clip: {fixed}",
        flush=True,
    )
    return str(fixed)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: gpt_sovits_batch.py jobs.json")
    job = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    polish = job.get("polish", False)
    engine = Path(job["engine"])
    reference_audio = prepare_reference_audio(job)
    sys.path.insert(0, str(engine))
    sys.path.insert(0, str(engine / "GPT_SoVITS"))
    os.chdir(engine)
    print("Loading GPT-SoVITS Python modules...", flush=True)
    from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config
    print("GPT-SoVITS Python modules loaded.", flush=True)

    config = TTS_Config(str(engine / "GPT_SoVITS" / "configs" / "tts_infer.yaml"))
    requested_device = str(job.get("device", "auto")).lower()
    device = "cpu"
    if requested_device in ("auto", "cuda", "gpu"):
        try:
            import torch
            if torch.cuda.is_available():
                device = "cuda"
        except Exception:
            device = "cpu"
    elif requested_device:
        device = requested_device
    config.device = device
    config.is_half = device.startswith("cuda")
    print(f"GPT-SoVITS device={config.device}, half={config.is_half}", flush=True)
    tts = TTS(config)
    output = Path(job["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    items = job.get("items") or [
        {"text": text, "filename": f"tts_{index:04d}.wav"}
        for index, text in enumerate(job.get("texts", []), 1)
    ]
    for index, item in enumerate(items, 1):
        text = item["text"]
        target = output / item["filename"]
        if target.exists() and target.stat().st_size > 1000:
            continue
        generator = tts.run({
            "text": text, "text_lang": "zh", "ref_audio_path": reference_audio,
            "prompt_lang": "zh", "prompt_text": job["prompt_text"],
            "temperature": 0.75, "top_k": 10, "top_p": 0.9,
            "repetition_penalty": 1.3, "speed_factor": float(job.get("speed", 1.0)),
            "text_split_method": "cut0", "media_type": "wav",
        })
        sample_rate, audio = next(generator)
        write_wav(target, audio, sample_rate)
        if polish:
            import subprocess
            polished_target = output / f"polished_{item['filename']}"
            subprocess.run([
                ffmpeg(), "-y", "-i", str(target),
                "-af", ("highpass=f=80,equalizer=f=3000:t=q:w=1:g=2,"
                        "compand=attacks=0.005:decays=0.05:"
                        "points=-80/-80|-30/-10|0/-3:gain=2,"
                        "loudnorm=I=-19:TP=-1.5:LRA=7"),
                "-ar", str(sample_rate), "-ac", "1", str(polished_target)
            ], capture_output=True, check=True, timeout=30)
            import shutil
            shutil.move(str(polished_target), str(target))
        print(f"GPT-SoVITS {index}/{len(items)}", flush=True)


if __name__ == "__main__":
    main()
