# AI Voice Interpreter

AI Voice Interpreter 是面向 macOS 的中文/英文实时语音翻译项目。Phase 3 新增 Meeting Bridge：在一对一会议中建立两条相互隔离的 LiveTranslate Session，使本地参与者 A 说中文时远端 B 只听英文，远端 B 说英文时 A 的耳机只听中文。

当前 Meeting Bridge 面向一对一、中文与英文、交替讲话。它使用 BlackHole 作为外部虚拟音频设备，没有自带虚拟驱动，也没有声学回声消除。现有 Local Demo、按句 HTTP One-shot、单向 Streaming、LiveTranslate 和 Modular 回退全部保留。

## 架构与四个音频端点

Meeting Bridge 同时运行两条独立 WSS Session，共用一个 `bridge_id`：

```text
A→B / local_to_remote
Physical Microphone
→ stateful soxr: device rate/stereo → 16 kHz mono S16LE
→ Gateway WSS /v1/stream, protocol 1.1, zh→en
→ qwen3.5-livetranslate-flash-realtime
→ 24 kHz mono PCM → device rate/stereo
→ BlackHole 2ch
→ meeting app Microphone
→ remote participant B hears English

B→A / remote_to_local
meeting app Speaker
→ BlackHole 16ch
→ first two channels downmixed, stateful soxr → 16 kHz mono S16LE
→ independent Gateway WSS /v1/stream, protocol 1.1, en→zh
→ qwen3.5-livetranslate-flash-realtime
→ 24 kHz mono PCM → headphones rate/stereo
→ Physical Headphones
→ local participant A hears Chinese
```

四个端点必须明确选择：

1. 我的麦克风：物理麦克风，只供 A→B 读取。
2. 发送给会议：BlackHole 2ch，只供 A→B 写入。
3. 接收会议声音：BlackHole 16ch，只供 B→A 读取。
4. 我的耳机：物理耳机，只供 B→A 写入；不会用 Mac 扬声器代替。

必须使用两个 BlackHole 设备，才能让“发给会议的英文译音”和“从会议收到的英文原音”处于不同逻辑总线。产品不会监听 BlackHole 2ch 作为翻译输入，也不会向 BlackHole 16ch 写入译音。

## Mac 安装

要求 macOS、Python 3.11–3.14、Homebrew、物理麦克风和物理耳机。

```bash
git clone https://github.com/27ruien/AI-Voice-Interpreter.git
cd AI-Voice-Interpreter
cp .env.example .env
make setup
```

安装 BlackHole 2ch 和 16ch：

```bash
brew install --cask blackhole-2ch
brew install --cask blackhole-16ch
```

官方安装器会要求 macOS 管理员密码。项目不会代替用户输入 sudo 密码、降低系统安全设置或把 BlackHole 安装包复制进仓库。安装后如果设备列表仍未出现，重启 Mac，再运行 `make meeting-doctor`。只有完成重启并重新检测到两个设备后，才可继续硬件路由验收。

首次使用麦克风时，在“系统设置 → 隐私与安全性 → 麦克风”允许 Terminal、iTerm 或 Python，然后完全退出并重新启动应用。

## Mac 配置与隐私

Mac 产品模式只连接 Gateway，不保存 DashScope API Key：

```dotenv
APP_MODE=real
INTERPRETER_MODE=remote_stream
AI_GATEWAY_BASE_URL=https://gridworks.cn/tool/ai-interpreter-api
AI_GATEWAY_TOKEN=
DASHSCOPE_API_KEY=
```

`AI_GATEWAY_TOKEN` 仅通过 `Authorization: Bearer ...` Header 发送，不进入 URL、音频路由文件、日志或报告。DashScope API Key 和 Workspace ID 只保存在服务器 `/srv/ai-voice-interpreter/server/.env`。

四端点选择保存在：

```text
~/Library/Application Support/AI Voice Interpreter/audio_routes.json
```

文件权限为 `600`，只含设备 stable key、音色和会议设置确认，不含 Token、API Key 或音频。stable key 当前使用规范化名称、Host API、输入/输出声道和采样率；PortAudio index 只在每次启动重新解析时使用。设备拔插或重启后无法唯一匹配时，应用不会选择任意设备，必须重新选择。

默认不保存会议原音或翻译音频。音频仅经过有界内存 Queue；Meeting Bridge 不使用 `afplay`。`afplay` 只保留给 Local One-shot Demo。所有诊断/Smoke 输出目录和 WAV/PCM/日志均被 Git 忽略。

## GUI：Meeting Setup 与启动

启动：

```bash
make run
```

在“工作模式”选择“会议桥接”，然后：

1. 在四个下拉框选择 Physical Microphone、BlackHole 2ch、BlackHole 16ch、Physical Headphones。
2. 点击“保存设置”。
3. 点击“运行音频自检”。
4. 在 Zoom、Microsoft Teams 或其他会议软件中设置：

   ```text
   Meeting Microphone: BlackHole 2ch
   Meeting Speaker: BlackHole 16ch
   ```

5. 勾选“已完成会议软件设置”。
6. 点击“Start Meeting Bridge”。

“打开会议设置说明”显示 Zoom/Teams/浏览器会议的通用设置；“复制设置摘要”只复制上面两行，不含 Token。应用不会自动修改第三方会议软件或 macOS 的全局默认音频设备。

每个方向卡片显示输入/输出、连接状态、输入电平、Partial/Final 字幕、首个设备写入延迟、音色、Provider、Fallback 和错误。全局状态包括 STARTING、RUNNING、DEGRADED、STOPPING、STOPPED 和 FAILED。停止或失败后两个方向恢复 Disconnected/Idle，可直接再次启动，无需重启 App。

Meeting Bridge 使用 Isolated Routing Mode：两路输入持续采集、两路输出持续写入独立设备。单向 Streaming 的 Safe Mode 和 Headphones Mode 在会议桥接中不适用。

## 启动保护与音频路由自检

无收费诊断：

```bash
make meeting-doctor
```

它检查 BlackHole 2ch/16ch、物理麦克风、物理耳机、Gateway readyz、Token 是否配置、同 Token 两条 WSS 容量、soxr 和保存的 Route Profile。只输出配置状态，不输出 Token，也不连接模型。

硬件自检：

```bash
make meeting-audio-doctor
make meeting-loopback-smoke
```

硬件自检执行物理麦克风 RMS/Peak、BlackHole 2ch Loopback、BlackHole 16ch Loopback、低音量耳机确认、Cross-route Isolation 和采样率转换。报告写入被忽略的 `meeting-audio-doctor-output/` 或 `meeting-loopback-output/`，`paid_model_calls` 始终为 0。

RouteGuard 会在打开麦克风和 Gateway 之前阻止以下情况：BlackHole 型号选反、物理端点误选虚拟设备、没有耳机、同一输入/输出冲突、采样率无法转换、设备无法打开、Gateway 不支持双 Session、Token 缺失或未确认会议软件设置。关键冲突不能忽略；失败时不会产生收费 Session。

如果设备被占用，先关闭可能独占该设备的录音/会议应用，再刷新设备并重跑自检。如果 BlackHole 不出现，确认两个 cask 都已安装并重启。如果耳机不出现，连接 USB/蓝牙/3.5 mm 耳机；MacBook Speakers 不会被当作耳机候选。

## 音频格式和循环保护

设备可使用 44.1 kHz 或 48 kHz。输入先下混再由 stateful SoXR 转换到 16 kHz mono S16LE；LiveTranslate 的 24 kHz mono S16LE 输出由另一条有状态重采样流转换到目标设备采样率，并复制到前两个输出声道。跨 Chunk 状态会保留，不会对每个 Chunk 独立重采样；不完整 Sample 会保留/计数，浮点输入会裁剪，未使用的多声道保持为零。

本轮没有声学回声消除，因此强制物理耳机。AudioLoopGuard 保证英文译音只写 BlackHole 2ch、中文译音只写耳机、会议原音只从 BlackHole 16ch 读取、物理麦克风只用于 A→B。检测到交叉路由会拒绝启动，不能用扬声器规避。

## Gateway 协议与双 Session Registry

公网入口：

```text
https://gridworks.cn/tool/ai-interpreter-api
→ wss://gridworks.cn/tool/ai-interpreter-api/v1/stream
```

原单向 Streaming 继续使用协议 `1.0`。Meeting Bridge 使用协议 `1.1`：

```json
{
  "type": "session.start",
  "protocol_version": "1.1",
  "request_id": "uuid",
  "bridge_id": "uuid",
  "session_role": "local_to_remote",
  "source_language": "zh",
  "target_language": "en",
  "mode": "meeting_bridge",
  "voice_mode": "standard",
  "voice": "Tina",
  "audio": {
    "format": "pcm_s16le",
    "sample_rate": 16000,
    "channels": 1,
    "chunk_ms": 100
  }
}
```

另一方向必须为 `session_role=remote_to_local`、`source_language=en`、`target_language=zh`。`bridge_id` 必须是 UUID；角色和语言必须匹配。同一 Token 默认只允许一个 Bridge 和两条不同角色 Session。Registry 只保存 Token 的 SHA-256 截断指纹，不保存完整 Token。方向关闭后独立释放，两个方向关闭或过期后 Bridge 记录归零。

生产 Provider 和 Meeting 音色由服务器控制；客户端不能指定任意模型。`/readyz` 显示协议版本、每 Token 容量、Bridge 支持、活动 Bridge/方向数和两路音色，但不返回密钥、完整 Token 或内部 Header。

## Provider、音色和回退

主链路为 `qwen3.5-livetranslate-flash-realtime`，默认 A→B 音色 Tina，B→A 音色 Ethan。两者由服务器配置。B→A 固定标准音色，不克隆远端参与者；A→B 默认标准音色，只有标准闭环完成后才可在服务器受控地启用 `clone_once`。声音复刻只能用于本人或已获授权的声音。

LiveTranslate 仅在连接/`session.update` 启动失败且尚未播放任何音频时，允许该方向自动切换一次 Modular Streaming。模块化回退会按当前方向设置 ASR 和 TTS language hint。另一方向继续工作并显示 DEGRADED。已播放音频后不会重放；两种流式 Provider 都失败时该方向停止，不会直通原音。Meeting Bridge 不自动回退到 HTTP One-shot，因为持续音频不适合按句 HTTP 重放。

现有单向链路保留：

```text
Local Streaming: Mac PCM → Gateway → LiveTranslate → Mac playback
Modular: VAD → paraformer-realtime-v2 → qwen-mt-flash → cosyvoice-v3-flash
HTTP One-shot: WAV → paraformer-v2 → qwen-mt-flash → cosyvoice-v3-flash → afplay
```

`INTERPRETER_MODE=local` 仍是 Mac 直连 DashScope 的开发回退，不是产品默认。旧的预登记音色 CLI 仍可用于 local/modular 开发链路：

```bash
.venv/bin/python -m ai_voice_interpreter.voice_enrollment \
  --audio-url "https://example.com/my-voice.wav" \
  --prefix myvoice \
  --language zh
```

## 服务器配置

服务器实际配置为 `/srv/ai-voice-interpreter/server/.env`，权限必须为 `600`：

```dotenv
STREAMING_ENABLED=true
STREAMING_PROTOCOL_VERSION=1.1
STREAMING_MAX_CONNECTIONS=2
STREAMING_MAX_CONNECTIONS_PER_TOKEN=2

STREAM_PIPELINE_PROVIDER=livetranslate
STREAM_PIPELINE_FALLBACK_PROVIDER=modular
ALLOW_STREAM_PIPELINE_OVERRIDE=false

LIVETRANSLATE_MODEL=qwen3.5-livetranslate-flash-realtime
LIVETRANSLATE_OUTPUT_MODALITIES=text,audio
LIVETRANSLATE_ENABLE_SOURCE_TRANSCRIPTION=true
LIVETRANSLATE_SOURCE_ASR_MODEL=qwen3-asr-flash-realtime

MEETING_BRIDGE_ENABLED=true
MEETING_BRIDGE_MAX_ACTIVE_PER_TOKEN=1
MEETING_BRIDGE_ALLOWED_LANGUAGE_PAIRS=zh:en,en:zh
MEETING_LOCAL_TO_REMOTE_VOICE=Tina
MEETING_LOCAL_TO_REMOTE_VOICE_MODE=standard
MEETING_REMOTE_TO_LOCAL_VOICE=Ethan
MEETING_REMOTE_TO_LOCAL_VOICE_MODE=standard
MEETING_BRIDGE_REGISTRY_TTL_SECONDS=120
```

`ASR_MODEL=paraformer-v2` 只用于 HTTP 文件识别，`STREAM_ASR_MODEL=paraformer-realtime-v2` 用于 Modular Streaming，翻译为 `qwen-mt-flash`，TTS 为 `cosyvoice-v3-flash`。不要用实时 ASR 模型覆盖 HTTP ASR。

## 自动化测试、本地模拟和 Soak

所有以下自动化测试都不调用收费 API：

```bash
make test
make lint
make server-test
make server-lint
make stream-test
make livetranslate-test
make meeting-test
make doctor
make meeting-doctor
git diff --check
```

纯离线双向模拟：

```bash
make meeting-bridge-smoke
```

默认强制 `--no-real-api`，建立相同 `bridge_id` 的两条 Mock Session，验证中文→英文、英文→中文、PCM 字节、两路输出隔离、无重复、无交叉和 Stop 清理。也可直接运行 `python -m ai_voice_interpreter.meeting_bridge_smoke`；入口保留 `--local-chinese-audio`、`--remote-english-audio`、`--play-local-translation`、`--capture-virtual-mic`、`--keep-files`、`--standard-voice`、`--clone-local-once`、`--json-report`、`--max-turns` 和 `--no-real-api` 参数。硬件 RouteGuard 未通过时不会进入真实模式。报告位于被忽略的 `meeting-bridge-smoke-output/`。Mock 成功不代表真实 Provider、BlackHole 或会议听感验收成功。

30 分钟双向 Mock Soak：

```bash
make meeting-bridge-soak
```

开发时可用 `make meeting-bridge-soak MEETING_SOAK_MINUTES=0.1`，但不能代替正式 30 分钟结果。Soak 检查反复 Start/Stop、两方向、Modular Fallback、内存趋势、Thread、活动 Stream、Gateway Session、Registry 和临时文件归零，不产生后台收费任务。

## 真实会议验收

只有 `make meeting-doctor` 和 `make meeting-audio-doctor` 都通过后，才允许短时真实测试。第一轮保持两个方向为标准音色，最多两条真实 Meeting Session、最多 6 个 Turn、总时长不超过 3 分钟。

人工步骤：

1. A 佩戴耳机，选择四个端点并通过 RouteGuard。
2. 会议软件 Microphone 选 BlackHole 2ch，Speaker 选 BlackHole 16ch。
3. 远端 B 加入真实会议，双方关闭其他可能占用设备的应用。
4. A 点击 Start Meeting Bridge。
5. A 说：“你好，我们今天主要讨论项目进度和下一步的交付计划。”
6. B 确认只听到英文语义：“Hello, today we will mainly discuss the project progress and the next delivery plan.”
7. B 说英文，A 确认耳机只听到中文译音。
8. 交替完成 10 个 Turn，确认无原音泄漏、自我重复翻译、交叉路由或双重播放。

Codex 无法代替真实远端参与者做主观确认。没有 B 的确认时，验收状态必须写 `NEEDS_USER_REMOTE_MEETING_CONFIRMATION`。

## 日志、资源和安全

每个 Bridge 记录非敏感 `bridge_id`、状态、活动方向、输入/输出秒数、Fallback 和错误计数；每个方向记录安全设备名、Gateway/上游 Session ID、首字幕/首翻译/首音频/首设备写入延迟、Queue Peak、Underrun、Backpressure、Turn、Provider 和请求 ID。

INFO 日志只记录 ID、设备安全名称、状态、文本长度、音频字节和耗时；不记录完整录音、Base64 音频、API Key、完整 Token 或完整用户文本。DEBUG 文本也必须截断。两路 InputStream、OutputStream、Queue、Gateway Client 和 Worker 完全独立；单路积压不能阻塞另一方向。退出会关闭全部流和连接。

## 部署与回滚

部署前先记录当前 commit、Gateway Container/Image/StartedAt、Nginx 状态、`server/.env` 权限，以及 ProjectAI Container/Image/StartedAt/端口。服务器部署：

```bash
cd /srv/ai-voice-interpreter
git pull --ff-only origin main
sudo docker compose -f server/compose.yaml build
sudo docker compose -f server/compose.yaml up -d
sudo docker compose -f server/compose.yaml ps
curl http://127.0.0.1:8100/healthz
curl http://127.0.0.1:8100/readyz
```

Compose 容器为 `ai-voice-interpreter-gateway`，内部端口只绑定 `127.0.0.1:8100`。Nginx 已支持公网 WSS；除非两条连接无法建立，否则不修改 Nginx。不得停止、重启或修改 `/srv/projectai`、ProjectAI `.env`、Nginx 路由或 `127.0.0.1:3100`。

如 Meeting Bridge 破坏旧链路，先设置 `MEETING_BRIDGE_ENABLED=false` 并只重建 Gateway。若仍有回归，恢复旧 commit/镜像并重新验证 healthz、readyz、单向 Streaming 和 HTTP One-shot。不要 force push、删除 Git 历史、执行 `docker system prune`、重启 Docker daemon 或服务器。

## 当前限制

当前 Meeting Bridge 面向一对一、中文与英文、交替讲话的双向翻译。它使用 BlackHole 作为外部虚拟音频设备，尚未实现自有虚拟驱动、声学回声消除、多人说话人识别以及双方同时抢话优化。

也不支持系统音频自动捕获、自动修改会议软件设置、自动切换 macOS 默认设备或把 BlackHole 打包进应用。蓝牙设备的动态采样率/断连取决于 CoreAudio；断开后对应方向会失败，恢复设备后应刷新并重连。
