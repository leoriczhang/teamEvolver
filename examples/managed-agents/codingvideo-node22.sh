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
# Live MA runs setup before its configured pip packages are visible.
if ! python3 -c 'import httpx, PIL; assert httpx.__version__ == "0.28.1"; assert PIL.__version__ == "11.3.0"'; then
  python3 -m pip install --disable-pip-version-check --no-input --quiet --timeout 20 --retries 2 \
    httpx==0.28.1 Pillow==11.3.0
fi
codingvideo_bootstrap_stage=runtime
printf 'CodingVideo bootstrap: runtime, Chromium and WebGL2\n'
