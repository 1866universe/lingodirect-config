# lingodirect-config 

Public configuration repository for **LingoDirect**

This repository is used to maintain application configuration required by the client-side integration and remote service connectivity.

## About LingoDirect

LingoDirect is a communication assistant and a bidirectional voice and text translation tool designed to simplify conversations between people with different native languages. It currently supports Persian and English.

The app is built around direct device-to-device (Peer-to-Peer) communication, allowing secure real-time conversations over a shared local Wi-Fi network or mobile hotspot without relying on intermediary servers.

LingoDirect uses two translation engines:

- **Online engine:** based on **NLLB-200 (nllb-ct2-1.3b)** and hosted on the project's dedicated server
- **Offline engine:** based on **ML Kit** language models and used for fallback and offline usage

If the network is unavailable or the private server capacity is full, the app automatically switches to the offline engine to keep translation available without interruption.

The project was originally built with limited hardware and infrastructure for personal use, and later released publicly to support broader communication needs. Online engine access depends on the server's permitted capacity, while the app and offline engine remain freely available.

## Features

- Fast voice and text translation between Persian and English
- Audio playback of translated text
- Direct communication over local Wi-Fi or mobile hotspot
- Automatic fallback to offline translation when needed
- Simple, lightweight, and ad-free design
- Automatic version checking against the latest stable GitHub release

## Download

The latest stable APK release is available here:

https://github.com/1866universe/lingodirect-config/releases/download/v1.1.1/LingoDirect-1.1.1.apk

## Overview

This repository provides:

- centralized configuration storage
- updateable runtime config values
- a simple source for client configuration retrieval

## Important

This README only contains public project information.

Internal operational details, private maintenance instructions, and sensitive project notes are documented separately in local/private files.