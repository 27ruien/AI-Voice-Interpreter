#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
probe_dir="$(mktemp -d /tmp/aivi-provider-permission.XXXXXX)"
local_aiff="${probe_dir}/probe.aiff"
local_wav="${probe_dir}/probe.wav"
remote_wav="/tmp/aivi-provider-permission.wav"
container_wav="/tmp/aivi-provider-permission.wav"

cleanup() {
  if [[ -e "${local_aiff}" ]]; then unlink "${local_aiff}"; fi
  if [[ -e "${local_wav}" ]]; then unlink "${local_wav}"; fi
  if [[ -d "${probe_dir}" ]]; then rmdir "${probe_dir}"; fi
  ssh gridworks.cn \
    "sudo docker exec -u 0 ai-voice-interpreter-gateway python -c 'from pathlib import Path; Path(\"${container_wav}\").unlink(missing_ok=True)' >/dev/null 2>&1 || true; if test -e '${remote_wav}'; then unlink '${remote_wav}'; fi" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

say -v Tingting -r 240 -o "${local_aiff}" "你好，很高兴见到你。"
afconvert -f WAVE -d LEI16@16000 -c 1 "${local_aiff}" "${local_wav}"
scp -q "${local_wav}" "gridworks.cn:${remote_wav}"
ssh gridworks.cn \
  "cd /srv/ai-voice-interpreter && sudo docker cp '${remote_wav}' ai-voice-interpreter-gateway:'${container_wav}' && sudo docker compose -f server/compose.yaml exec -T gateway python -m server.provider_permission_smoke --audio '${container_wav}'"
