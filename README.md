# LingoDirect Configuration & Runtime Protocols

LingoDirect is an open, modular machine translation and cross-lingual communication architecture designed for real-time translation pipelines, speech-to-speech integration, and deterministic linguistic mapping.

---

## Overview
LingoDirect is a communication assistant and a bidirectional voice and text translation tool designed to simplify conversations between people with different native languages. 
- **Current Support:** Persian and English.
- **Connectivity:** Peer-to-Peer (P2P) communication, allowing secure real-time conversations over shared local Wi-Fi or mobile hotspots without intermediary servers.

---

## Key Modules & Responsibilities

### Translation Engines
- **Online engine:** Powered by **NLLB-200 (nllb-ct2-1.3b)**, hosted on the project's dedicated server.
- **Offline engine:** Based on **ML Kit** language models, providing automatic fallback when the network is unavailable or server capacity is reached.

### Features
- Fast voice and text translation (Persian <-> English).
- Audio playback of translated text.
- Simple, lightweight, and ad-free design.
- Automatic version checking against the latest stable GitHub release.

---

## Architectural Principles
1. **Decoupled Architecture:** Separates runtime execution logic from configuration state and language definition assets.
2. **Deterministic Alignment:** Designed to interface seamlessly with structured positional-sequential mapping models.
3. **Low-Latency Streaming:** Specifications tuned for interactive, turn-by-turn conversational interfaces.

---

## System Integration & Download Guide
Configurations defined here are ingested directly by active LingoDirect deployments. 

**Official APK files** are available in the Assets section of the GitHub releases repository:
[Download LingoDirect Releases](https://github.com/1866universe/lingodirect-config/releases)

[راهنمای فارسی](README_FA.md)

---
*1866Universe 2026 ©*
