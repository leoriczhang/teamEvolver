#!/usr/bin/env bash
set -euo pipefail
mkdir -p /mnt/session/outputs
exec >> /mnt/session/outputs/codingvideo-bootstrap.log 2>&1
codingvideo_bootstrap_stage=node
printf '{"status":"initializing"}\n' > /mnt/session/outputs/codingvideo-bootstrap-status.json
trap 'codingvideo_bootstrap_exit=$?; printf "{\"exitCode\":%s,\"stage\":\"%s\"}\n" "$codingvideo_bootstrap_exit" "$codingvideo_bootstrap_stage" > /mnt/session/outputs/codingvideo-bootstrap-status.json; test -z "${node_archive:-}" || rm -f "$node_archive"' EXIT
export npm_config_registry=https://registry.npmmirror.com
export PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
export PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=45000
printf 'CodingVideo bootstrap: Node 22\n'
# Use a clean prefix: overlaying MA's existing npm leaves incompatible modules.
node_prefix=/opt/codingvideo-node-22
if ! test -x "$node_prefix/bin/node"; then
  case "$(uname -m)" in
    x86_64)
      node_arch=x64
      node_sha=df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de
      ;;
    aarch64)
      node_arch=arm64
      node_sha=a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f
      ;;
    *) echo "Unsupported Node architecture" >&2; exit 1 ;;
  esac
  node_archive="$(mktemp)"
  node_downloaded=0
  for node_source in https://registry.npmmirror.com/-/binary/node https://nodejs.org/dist; do
    if curl --fail --silent --show-error --location --connect-timeout 10 --max-time 45 \
      "${node_source}/v22.23.3/node-v22.23.3-linux-${node_arch}.tar.xz" --output "$node_archive" \
      && printf '%s  %s\n' "$node_sha" "$node_archive" | sha256sum --check --status; then
      node_downloaded=1
      break
    fi
  done
  test "$node_downloaded" = 1 || { echo "Node download or SHA256 validation failed"; exit 1; }
  mkdir -p "$node_prefix"
  tar -xJf "$node_archive" -C "$node_prefix" --strip-components=1
  rm -f "$node_archive"
fi
# Agent tool shells use /opt/node/bin; resolve npm's CLI into its clean package.
for node_tool in node npm npx; do
  ln -sf "$node_prefix/bin/$node_tool" "/opt/node/bin/$node_tool"
done
export PATH="$node_prefix/bin:/opt/node/bin:/opt/python/bin:$PATH"
hash -r
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'
codingvideo_bootstrap_stage=python-dependencies
printf 'CodingVideo bootstrap: Python dependencies\n'
# Declare these pinned dependencies in runtime_binding.packages so MA prepares
# them before setup. Keep this bounded fallback for older Environment snapshots.
if ! python3 -c 'import httpx, PIL, numpy, scipy, librosa; assert httpx.__version__ == "0.28.1"; assert PIL.__version__ == "11.3.0"; assert numpy.__version__ == "2.2.6"; assert scipy.__version__ == "1.15.3"; assert librosa.__version__ == "0.11.0"'; then
  python3 --version
  if command -v uv >/dev/null; then
    timeout 600 env UV_HTTP_TIMEOUT=20 UV_HTTP_RETRIES=2 uv pip install \
      --python "$(command -v python3)" --only-binary=:all: \
      --index-url https://mirrors.aliyun.com/pypi/simple httpx==0.28.1 Pillow==11.3.0 numpy==2.2.6 scipy==1.15.3 librosa==0.11.0
  else
    timeout 600 python3 -m pip install --disable-pip-version-check --no-input \
      --only-binary=:all: --timeout 20 --retries 2 \
      --index-url https://mirrors.aliyun.com/pypi/simple httpx==0.28.1 Pillow==11.3.0 numpy==2.2.6 scipy==1.15.3 librosa==0.11.0
  fi
fi
codingvideo_bootstrap_stage=ffmpeg
printf 'CodingVideo bootstrap: pinned FFmpeg wheel\n'
ffmpeg_requirements="$(mktemp)"
printf '%s\n' 'imageio-ffmpeg==0.6.0 --hash=sha256:c7e46fcec401dd990405049d2e2f475e2b397779df2519b544b8aab515195282 --hash=sha256:1d47bebd83d2c5fc770720d211855f208af8a596c82d17730aa51e815cdee6dc' > "$ffmpeg_requirements"
if command -v uv >/dev/null; then
  timeout 300 env UV_HTTP_TIMEOUT=20 UV_HTTP_RETRIES=2 uv pip install \
    --python "$(command -v python3)" --only-binary=:all: --require-hashes \
    --index-url https://mirrors.aliyun.com/pypi/simple -r "$ffmpeg_requirements"
else
  timeout 300 python3 -m pip install --disable-pip-version-check --no-input \
    --only-binary=:all: --require-hashes --timeout 20 --retries 2 \
    --index-url https://mirrors.aliyun.com/pypi/simple -r "$ffmpeg_requirements"
fi
rm -f "$ffmpeg_requirements"
python3 - <<'CV_FFMPEG_INSTALL'
import hashlib, json, platform, subprocess
from pathlib import Path
import imageio_ffmpeg
assert imageio_ffmpeg.__version__ == "0.6.0"
binary = Path(imageio_ffmpeg.get_ffmpeg_exe()).resolve()
digest = hashlib.sha256(binary.read_bytes()).hexdigest()
if platform.machine() == "x86_64":
    assert digest == "e7e7fb30477f717e6f55f9180a70386c62677ef8a4d4d1a5d948f4098aa3eb99"
for prefix in ("/usr/local/bin", "/opt/node/bin", "/opt/python/bin"):
    link = Path(prefix) / "ffmpeg"
    link.unlink(missing_ok=True)
    link.symlink_to(binary)
probe = subprocess.check_output(["ffmpeg", "-hide_banner", "-h", "filter=alimiter"], text=True)
assert "latency" in probe
subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-t", "0.1", "-af", "alimiter=limit=0.841395:level=false:latency=true",
                "-f", "null", "-"], check=True)
version = subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0]
proof = {"ready": True, "package": "imageio-ffmpeg==0.6.0", "binaryPath": str(binary),
         "binarySha256": digest, "ffmpeg": version, "limiterLatencyVerified": True}
Path("/mnt/session/outputs/codingvideo-ffmpeg-ready.json").write_text(json.dumps(proof))
print(json.dumps(proof))
CV_FFMPEG_INSTALL
hash -r

codingvideo_bootstrap_stage=runtime
printf 'CodingVideo bootstrap: runtime, Chromium and WebGL2\n'
