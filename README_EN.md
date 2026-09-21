<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-e1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases/tag/pro-R1.1.0.0"><img src="https://img.shields.io/badge/releases-R1.1.0.0-3b82f6" alt="releases R1.1.0.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LisseldeE/LANSyncBox" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue?logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/platform-Linux-orange?logo=linux" alt="Platform">
</p>

<p align="center">
  <a href="#project-information">Project Info</a> |
  <a href="#system-support">Download</a> |
  <a href="#usage">Usage</a> |
  <a href="#sync-logic">Sync Logic</a> |
  <a href="#installation">Installation</a> |
  <a href="#open-source-license">License</a>
</p>

> **Version Note**: This branch is the **Pro** edition of [**LANSyncBox**](https://github.com/LisseldeE/LANSyncBox/tree/main). The decentralized refactor has been completed and, after multiple rounds of internal testing, the features are largely complete. If you encounter any issues while using it, please report them in a timely manner.

## Project Introduction

LANSyncBox Pro is a cross-platform, LAN-focused real-time file synchronization tool for multiplayer collaboration. It requires no public network: it supports large-file streaming transfer, serialized sending, and broken-transfer resumption, ensuring reliable data and stable transfer under multi-connection, high-concurrency scenarios. Compared to the legacy version, Pro adopts a new decentralized distributed architecture, eliminating the single-point-of-failure that affects the entire link. It also introduces several new capabilities: **Delivery** (cross-device copy & paste), top-edge drag-and-drop **quick-add**, Collect mode, permission management, and more.

## Version Comparison

| Feature | Standard | Pro |
| :--- | :---: | :---: |
| Real-time file sync | ✔ | ✔ |
| Room sharing (6-digit code · password check) | ✔ | ✔ |
| Large file transfer (streaming · resume) | ✔ | ✔ |
| File operations (add/edit/delete · read-only preview) | ✔ | ✔ |
| Chinese / English UI switching | ✔ | ✔ |
| **Delivery · cross-device copy & paste** | — | ⭐ **New** |
| **Text clipboard broadcast** | — | ⭐ **New** |
| **Image / file peer-to-peer delivery** | — | ⭐ **New** |
| **Top quick-add** | — | ⭐ **New** |
| **Sync / Collect modes** | — | ⭐ **New** |
| **Decentralized mesh sync** | — | ⭐ **New** |

## Screenshots

| Main Interface | Sync Interface |
| :---: | :---: |
| ![Main Interface](https://lisseldee.github.io/assets/images/webp/1p-3.webp) | ![Sync Interface](https://lisseldee.github.io/assets/images/webp/1p-4.webp) |

## Project Information

- **Project Name**: LANSyncBox Pro
- **Project Author**: Lisselde_E
- **License**: GNU General Public License v3.0
- **Project Homepage**: https://lisseldee.github.io/#1
- **Project Repository**: https://github.com/LisseldeE/LANSyncBox/tree/pro

## Core Features (Existing)

- **Real-time Sync**: additions, edits, deletions, and renames sync to all clients instantly; auto-aligns differences on first connect
- **Room Sharing**: custom 6-digit room codes, optional password protection, sync-logic version compatibility check when joining
- **Large File Transfer**: streaming chunked transfer with resume support; transfers fail without corrupting files; up to 5 files in parallel, same-name pulls serialized for byte consistency
- **File Operations**: add, create, copy, cut, paste, delete, rename; double-click for read-only preview
- **Interface**: smooth Qt6 UI, real-time Chinese/English switching, visible transfer progress

## System Support

<div align="center">

| Operating System | x64 (AMD64) | ARM64 |
| :--- | :---: | :---: |
| Windows | ✅ | ❌ |
| Linux | ✅ | ❌ |
| macOS | ❌ | ❌ |

</div>

> Currently covers Windows 10/11 and Linux (x64) only; macOS support is on the roadmap.

## Download

<p align="center">
  <a href="https://apps.microsoft.com/detail/9n4g6w3rm3q6?referrer=appbadge&mode=full" target="_blank"  rel="noopener noreferrer">
	  <img src="https://get.microsoft.com/images/en-us%20dark.svg" width="200"/>
  </a>
</p>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/blob/pro/downloadwizard/en.md#LANSyncBox Pro">
    <img src="https://img.shields.io/badge/GitHub%20Releases-Download-181717?style=for-the-badge&logo=github&logoColor=white" alt="GitHub Releases"/>
  </a>
</p>

## Usage

### Host (Create Connection)

1. Click "Create Connection" button
2. Optional: Set password protection
3. Click create to enter sync status window

### Client (Join Connection)

1. Click "Join Connection" button
2. Enter or select room code
3. If password is required, you will be guided to the password input screen
4. Join the room to start syncing

## Sync Logic

### Endpoint Roles

| End | Role |
| :--- | :--- |
| **Any end (peer node)** | Each end independently maintains its operation list and file state (vector clocks); changes are dispatched mesh-wide in real-time; if any end goes offline, the rest keep syncing and converging |
| **Joining (decentralized)** | No host required online: any online end can answer discovery, verification, and guide new ends in; full alignment happens automatically after joining |
| **Read/Write Permissions** | Managed by the room creator (host end) for each connected client |
| **Conflict** | A three-layer safeguard — version vector, logical clock, and timestamp — ensures correctness of conflict resolution |

### Sync Mechanisms

| Mechanism | Description |
| :--- | :--- |
| **Real-time Sync** | File changes recorded via operation list and dispatched in real-time |
| **Transfer Protocol** | TCP + custom protocol |
| **Streaming Transfer** | Chunked streaming to avoid loading entire files into memory |
| **Concurrency Control** | Up to 5 files transfer in parallel; concurrent in-flight pulls of the same file are auto-serialized, with a post-write staleness check guaranteeing byte convergence |
| **Resumable Sending** | Interrupted sends only resume the remaining bytes without resending what was already sent; brief back-off when the receiver is busy keeps latency realistic |
| **Integrity Check** | Validates file size on completion, discards incomplete files |
| **Cancel & Cleanup** | Active cancellation supported (window close / user cancel); interrupted transfers immediately remove `.tcp_*.part` temp files, leaving no partial artifacts in sync |
| **Version Gate** | Joining validates only the internal sync logic version (`SYNC_LOGIC_VERSION`); UI / display-only changes don't force everyone to upgrade |
| **Node Offline** | Decentralized mesh: if any node disconnects, the rest keep syncing and converging; incremental sync resumes automatically on reconnect |

## Change Log

See [Changelog](https://github.com/LisseldeE/LANSyncBox/blob/pro/CHANGELOG.md)

## Tech Stack

- Python 3.x
- Qt6 (PySide6)
- Custom TCP Protocol

## Installation

### System Requirements
- Windows 10 or later (64-bit)
- Linux x64 (amd64) distributions

### Installation Methods
- **Windows**: Download the installer from [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases), then run it
- **Linux (deb package)**: Download the architecture-appropriate `.deb` package from [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases), then install it from the terminal:
  ```bash
  sudo apt install -y ./lansyncbox_*.deb
  ```

### Running
- **Windows**: After installation, launch LANSyncBox Pro from the Start menu or desktop shortcut
- **Linux**: Launch LANSyncBox from the app menu (Activities); if it reports missing system dependencies at runtime, run:
  ```bash
  sudo apt install -y libxcb-cursor0 libgl1 libxkbcommon-x11-0
  ```

## Open Source License

This project uses the GNU General Public License v3.0, see [LICENSE](https://github.com/LisseldeE/LANSyncBox/blob/pro/LICENSE) file for details.

## Privacy Policy

This project does not collect any user data, see [Privacy Policy](https://github.com/LisseldeE/LANSyncBox/blob/pro/privacy_policy.md) file for details.

## Commercial Cooperation

This project is open-sourced under the GPL-3.0 license. If you wish to use it in a closed-source commercial scenario, or require custom development and technical support, feel free to contact us through the following methods:

**Contact**: Lisselde.E@outlook.com

**Cooperation Process**:
1. Describe your usage scenario and requirements
2. Confirm the scope of authorization and fees
3. Sign an authorization agreement
4. Obtain commercial authorization and technical support

**Authorization Scope**:
- Closed-source commercial license
- Enterprise deployment license
- Custom development and technical support

## Feedback

**In Beta testing — if you have any questions or new ideas, feel free to contact me!**

Issues and Pull Requests are welcome!
Email: Lisselde.E@outlook.com

## Support Me

If you find this tool useful, feel free to tip me on **ifdian (爱发电)** to support further development. Thank you so much for your kindness!

<a href="https://ifdian.net/a/lisseldee">
  <img src="https://img.shields.io/badge/ifdian-Support_me-018E96?style=for-the-badge" alt="Support me">
</a>