#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
venv_python="${project_dir}/.venv/bin/python"

if ! command -v python3 >/dev/null 2>&1; then
  echo "错误：未找到 Python 3。请先安装 Python 3.11 或更高兼容版本。" >&2
  exit 1
fi

python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11) or sys.version_info >= (3, 15))'; then
  echo "错误：当前 Python ${python_version} 不受支持，需要 Python 3.11–3.14。" >&2
  exit 1
fi

if [[ ! -x "${venv_python}" ]]; then
  echo "错误：未找到 .venv。请先运行 make setup。" >&2
  exit 1
fi

if ! "${venv_python}" -c 'import PySide6, dashscope, httpx, sounddevice, soundfile' >/dev/null 2>&1; then
  echo "错误：虚拟环境依赖不完整。请重新运行 make setup。" >&2
  exit 1
fi

cd "${project_dir}"
exec "${venv_python}" -m ai_voice_interpreter.main
