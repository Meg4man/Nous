```
███╗   ██╗ ██████╗ ██╗   ██╗███████╗
████╗  ██║██╔═══██╗██║   ██║██╔════╝
██╔██╗ ██║██║   ██║██║   ██║███████╗
██║╚██╗██║██║   ██║██║   ██║╚════██║
██║ ╚████║╚██████╔╝╚██████╔╝███████║
╚═╝  ╚═══╝ ╚═════╝  ╚═════╝ ╚══════╝
```

> **Before you ask — Nous knows.**

Nous is a universal, proactive AI ecosystem that lives on your own hardware. It learns from you, adapts to your routines, and acts before you need to ask. Named after the ancient Greek concept of the universal intelligent mind. Claude is used for designing, layouts and the website. This way I can work on the prototype whilst the website, apps are being designed and it saves me time so we all can use Nous faster.

No cloud. No subscription. No compromise.

---

## What makes Nous different

| Other AI assistants | Nous |
|---|---|
| Your data lives on their servers | Your data never leaves your network |
| Monthly subscription | Free forever, once built |
| Reactive — waits for you to ask | Proactive acts before you ask(with permission)|
| Cloud-dependent | Runs fully offline/online(depends on configuration and custom additions) |
| Closed source | Open source, every line readable and editable to your own desires |

---

## Architecture

Nous is built in five independent layers. Each has one job.

```
┌─────────────────────────────────────────────────────┐
│  PERCEPTION    Microphones · Cameras · Motion        │
│                OpenWakeWord · Faster-Whisper         │
├─────────────────────────────────────────────────────┤
│  INTELLIGENCE  Local LLM · STT · TTS · Vision       │
│                Llama 3.x / Qwen 2.5 via Ollama      │
├─────────────────────────────────────────────────────┤
│  MEMORY        Vector DB · Pattern storage           │
│                pgvector / ChromaDB + Federated ML   │
├─────────────────────────────────────────────────────┤
│  ACTION        Smart home · Device control           │
│                Home Assistant · Zigbee2MQTT          │
├─────────────────────────────────────────────────────┤
│  INTERFACE     Voice · Mobile app · Web dashboard   │
│                Flutter (Android) · SvelteKit         │
└─────────────────────────────────────────────────────┘
```

---

## AI Model Stack

All models are open source and run locally. Zero API calls.

| Role | Model | Runtime |
|---|---|---|
| Language model (LLM) | Llama 3.x / Qwen 2.5 | Ollama |
| Speech-to-text (STT) | Faster-Whisper Large-v3 | Local GPU |
| Text-to-speech (TTS) | Piper / Coqui XTTS v2 | Local GPU |
| Vision (VLM) | Moondream2 / LLaVA | Local GPU |
| Memory | pgvector / ChromaDB | PostgreSQL |
| Orchestration | LangChain / LlamaIndex | Local |
| Wake word | OpenWakeWord | Local CPU |
| Home control | Home Assistant | Docker |
| Smart lights | Zigbee2MQTT | Docker |
| IR control | Broadlink RM4 Mini | Local |
| Remote access | WireGuard / Tailscale | Local |

### VRAM Budget — RTX 3090 (24 GB)

```
LLM (Qwen 32B / Llama 8B)   ████████████████░░░░░   8–16 GB
Faster-Whisper STT           ██░░░░░░░░░░░░░░░░░░░   ~2 GB
Moondream2 Vision            ██░░░░░░░░░░░░░░░░░░░   ~2 GB
Piper TTS                    █░░░░░░░░░░░░░░░░░░░░   ~1 GB
System / overhead            ██░░░░░░░░░░░░░░░░░░░   ~2 GB
────────────────────────────────────────────────────
Total                                                ~21 GB / 24 GB
```

---

## Hardware — Prototype Build

| Recommended hardware |
|---|---|---|---|
| GPU | NVIDIA RTX 3090 | 24 GB VRAM(21GB Vram needed)|
| CPU | AMD Ryzen 7 7700X | Intel core I5-13600k
| RAM | DDR5 32 GB |
| Storage | at least 1 TB for models, temporary storage and possible programs |

### Peripherals & Sensors

| Component | Model | Role | Est. |
|---|---|---|---|
| Smart Speaker / Mic | M5Stack ATOM Echo | In-room voice interaction |
| Camera | TP-Link Tapo C120 | Presence detection, activity recognition |
| Motion Sensor | TP-Link Tapo T100 | Triggers presence detection |
| Smart Lights | IKEA TRADFRI (Zigbee) | Controllable lighting |
| Zigbee USB Dongle | SONOFF Zigbee 3.0 | USB Zigbee coordinator |
| IR Blaster | Broadlink RM4 Mini | TV, A/C, IR devices |

---

## Platform Support

| Platform | Priority | Status |
|---|---|---|
| Linux | High | Prototype target |
| Android | High | Companion app — prototype target |
| Windows | medium | after prototype|
| macOS | Medium | Future release |
| iOS | Lower | Future release after macOS |

---

## Full Software Stack

### Server — Ubuntu Server 24.04 LTS

| Software | Role |
|---|---|
| Ubuntu Server 24.04 LTS | Server OS |
| Ollama | Runs and manages local LLMs |
| Faster-Whisper | Local STT with GPU acceleration |
| Piper / Coqui XTTS v2 | Local TTS voice synthesis |
| Moondream2 / LLaVA | Local vision model |
| LangChain / LlamaIndex | Agent orchestration |
| pgvector (PostgreSQL) | Vector memory database |
| OpenWakeWord | Local wake word detection |
| Home Assistant (Docker) | Smart home automation |
| Zigbee2MQTT | Bridges Zigbee to Home Assistant |
| Mosquitto | MQTT message broker |
| Docker + Docker Compose | Service containerisation |
| NVIDIA Container Toolkit | GPU access inside Docker |
| WireGuard / Tailscale | Secure remote tunnel |
| Portainer | Docker management UI |
| SvelteKit / Next.js | Web dashboard framework |

---

## Roadmap

- [x] Project vision & architecture
- [x] Technical blueprint (v2.0)
- [x] Marketing website
- [x] GitHub repository setup
- [ ] LLM stack running via Ollama
- [ ] Faster-Whisper STT integration
- [ ] Piper TTS integration
- [ ] Moondream2 vision model
- [ ] OpenWakeWord wake detection
- [ ] Voice interface — end to end
- [ ] Home Assistant integration
- [ ] Zigbee light control
- [ ] Flutter Android companion app
- [ ] SvelteKit web dashboard
- [ ] Federated learning across devices
- [ ] Linux desktop support
- [ ] Public beta

---

## Getting Started

> Nous is currently in active prototype development. Full setup documentation will be published as each component ships. Track progress on the [roadmap](#roadmap) above.

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.

One hard rule: **every contribution must be able to run fully locally.** No PR that introduces cloud dependency, external API calls, or centralised data collection will be merged.

---

## Philosophy

Nous is built on the belief that intelligence should belong to everyone — not rented from a platform, not paid for with your privacy, not dependent on a server farm you don't control.

It will always be free. Not free until we find a business model. Free.

---

## License

Nous is released under the [Nous Source License v1.0](LICENSE).

Free for personal, non-commercial use. Commercial use requires a separate licence — contact [Ninurta van Mierlo](https://github.com/Meg4man).

---

*Built by [Ninurta van Mierlo](https://github.com/Meg4man) — one person, one question: can I actually build this?*
