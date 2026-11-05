import shutil
import subprocess

import pytest

from team_replay.evidence import artifact_evidence


def test_real_video_and_audio_are_decoded_and_sampled(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    path = tmp_path / "micro.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=teal:s=160x90:r=10:d=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-strict", "-2", "-threads", "1", "-shortest", str(path)],
        check=True, capture_output=True, timeout=30,
    )
    actual = artifact_evidence("micro.mp4", path.read_bytes())
    assert actual["available"] and actual["decodable"]
    assert (actual["width"], actual["height"]) == (160, 90)
    assert actual["audio_tracks"] == 1
    assert 0.9 <= actual["duration_seconds"] <= 1.2
    assert len(actual["images"]) == 3
    assert all(image.startswith("data:image/jpeg;base64,") for image in actual["images"])
    invalid = artifact_evidence("claimed-video.mp4", b"The Agent says the video exists")
    assert not invalid["available"] and not invalid["decodable"]
