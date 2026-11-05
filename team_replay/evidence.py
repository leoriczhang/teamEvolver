"""Deterministic evidence from actual artifact bytes; no Agent assertions."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def artifact_evidence(name, data):
    suffix = Path(name).suffix.lower()
    if suffix in {".json", ".md", ".txt", ".csv"}:
        if len(data) > 1024 * 1024:
            return {"kind": "text", "available": False, "reason": "text evidence exceeds limit"}
        try:
            content = data.decode("utf-8")
            value = json.loads(content) if suffix == ".json" else content
            return {"kind": "json" if suffix == ".json" else "text", "available": True, "content": value}
        except (UnicodeError, ValueError):
            return {"kind": "text", "available": False, "reason": "artifact is not valid text/JSON"}
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        if len(data) > 8 * 1024 * 1024:
            return {"kind": "image", "available": False, "reason": "image evidence exceeds limit"}
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[suffix]
        return {"kind": "image", "available": True,
                "images": ["data:" + mime + ";base64," + base64.b64encode(data).decode()]}
    if suffix not in {".mp4", ".webm", ".mov", ".mp3", ".wav", ".m4a"}:
        return {"kind": "binary", "available": True, "size": len(data)}
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        return {"kind": "media", "available": False, "reason": "ffprobe/ffmpeg unavailable"}
    with tempfile.TemporaryDirectory(prefix="te-media-evidence-") as tmp:
        root = Path(tmp)
        path = root / ("artifact" + suffix)
        path.write_bytes(data)
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                capture_output=True, timeout=30, check=True,
            )
            info = json.loads(probe.stdout)
            streams = info.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            duration = float(info.get("format", {}).get("duration") or 0)
            # Decode actual tracks rather than trusting container metadata alone.
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                           capture_output=True, timeout=60, check=True)
            result = {"kind": "media", "available": True, "decodable": True, "duration_seconds": duration,
                      "audio_tracks": len(audio), "streams": [
                          {k: s[k] for k in ("codec_type", "codec_name", "width", "height", "sample_rate", "channels")
                           if k in s} for s in streams]}
            if video:
                result.update(width=video.get("width"), height=video.get("height"), images=[])
                for index, fraction in enumerate((0.1, 0.5, 0.9)):
                    frame = root / f"frame-{index}.jpg"
                    subprocess.run(
                        ["ffmpeg", "-v", "error", "-ss", str(duration * fraction), "-i", str(path),
                         "-frames:v", "1", "-vf", "scale=960:-2", str(frame)],
                        capture_output=True, timeout=30, check=True,
                    )
                    if frame.exists():
                        encoded = base64.b64encode(frame.read_bytes()).decode()
                        result["images"].append("data:image/jpeg;base64," + encoded)
                if not result["images"]:
                    result.update(available=False, reason="video frame extraction failed")
            return result
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"kind": "media", "available": False, "decodable": False, "reason": "media decode/probe failed"}
