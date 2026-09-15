<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-e1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/badge/releases-R1.0.0.0-3b82f6" alt="releases R1.0.0.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LisseldeE/LANSyncBox" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue?logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/platform-Linux-orange?logo=linux" alt="Platform">
</p>

<p align="center">
  <a href="#screenshots">Screenshots</a> |
  <a href="#project-information">Project Info</a> |
  <a href="#system-support">Download</a> |
  <a href="#usage">Usage</a> |
  <a href="#sync-logic">Sync Logic</a> |
  <a href="#installation">Installation</a> |
  <a href="#open-source-license">License</a>
</p>

> ⚠️ **Beta Notice**: This branch (`pro`) is the **Pro** edition of [**LANSyncBox**](https://github.com/LisseldeE/LANSyncBox/tree/main), under active development. Features are not yet fully stable and may change significantly. It is currently in the Beta testing stage — do not use it in production.

## Project Introduction

LANSyncBox Pro is the upgraded version of [LANSyncBox](https://github.com/LisseldeE/LANSyncBox/tree/main). It focuses on LAN multi-user collaboration. Compared to the standard edition, Pro is introducing several new capabilities — **Delivery** for cross-device copy & paste, top-edge **quick-add** by drag-and-drop, Collect mode, and more — under continuous development.

## Version Comparison

> The standard edition keeps all of its existing features; the Pro edition layers new LAN collaboration capabilities on top (Beta roadmap, partially listed, details may shift).

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

## Screenshots

| Main Interface | Sync Interface |
| :---: | :---: |
| ![Main Interface](https://lisseldee.github.io/assets/images/webp/1p-3.webp) | ![Sync Interface](https://lisseldee.github.io/assets/images/webp/1p-4.webp) |

## New Highlights (In Progress)

> Beyond file sync, the Pro edition is adding the following capabilities (in development, listed only partially).

| Feature | Description |
| :--- | :--- |
| **Delivery · Cross-device Copy & Paste** | Copy on device A, hit **Ctrl+V** on device B — done. **Text** is broadcast by the host into every client's system clipboard; **images/files** are **pulled peer-to-peer** from the copying device and delivered to your local folder |
| **Top Quick-Add** | Drag files/folders onto the **top edge of the screen** to add them to the sync list instantly, without reaching into the window |
| **Distributed File Transfer** | File metadata is distributed by the host, but the bytes flow **directly between the copying and receiving peers**, bypassing host forwarding |
| **Sync / Collect Modes** | On top of real-time sync, a new **Collect mode**: clients submit files to the host only, without broadcasting to other clients |

> The above are planned Beta changes; specifics and progress may shift during development. Refer to actual releases.

## Core Features (Existing)

- **Real-time Sync**: additions, edits, deletions, and renames sync to all clients instantly; auto-aligns differences on first connect
- **Room Sharing**: custom 6-digit room codes, optional password protection, version compatibility check when joining
- **Large File Transfer**: streaming chunked transfer with resume support; transfers fail without corrupting files; up to 5 files at once, auto-cancels on change
- **File Operations**: add, create, copy, cut, paste, delete, rename; double-click for read-only preview
- **Interface**: smooth Qt6 UI, real-time Chinese/English switching, visible transfer progress

## Project Information

- **Project Name**: LANSyncBox Pro
- **Project Author**: Lisselde_E
- **License**: GNU General Public License v3.0
- **Project Homepage**: https://lisseldee.github.io/#1
- **Project Repository**: https://github.com/LisseldeE/LANSyncBox/tree/pro

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
  <a href="https://github.com/LisseldeE/LANSyncBox/releases">
    <img src="https://img.shields.io/badge/GitHub-Releases-181717?style=flat-square&logo=github&logoColor=white" alt="GitHub Releases">
  </a>
  &nbsp;&nbsp;
  <a href="https://gitee.com/Lisselde_E/LANSyncBox/releases">
    <img src="https://img.shields.io/badge/Gitee-Mirror-C71D23?style=flat-square&logo=gitee&logoColor=white" alt="Gitee Mirror">
  </a>
</p>

> 💡 Recommended for users in China: Gitee Mirror

## Usage

### Host (Create Connection)

1. Click "Create Connection" button
2. Optional: Set password protection
3. Click create to enter sync status window

### Client (Join Connection)

1. Click "Join Connection" button
2. Enter or select room code
3. If password required, enter directly in join dialog for pre-verification
4. Verification failures are displayed directly in the join dialog, allowing immediate retry with corrected info
5. Click connect - automatic full sync from host

## Sync Logic

### Endpoint Roles

| End | Role |
| :--- | :--- |
| **Host** | Maintains file list, syncs changes to all clients in real-time; manages read/write permissions of each client; receives client files and forwards to others |
| **Client** | Uploads file changes to host (not directly to other clients); incremental sync after manual reconnect |
| **Conflict** | The version with the latest modification time wins |

### Sync Mechanisms

| Mechanism | Description |
| :--- | :--- |
| **Real-time Sync** | File changes recorded via operation list and dispatched in real-time |
| **Transfer Protocol** | TCP + custom protocol |
| **Streaming Transfer** | Chunked streaming to avoid loading entire files into memory |
| **Concurrency Control** | Max 5 files transferred simultaneously to optimize resource usage |
| **Resumable Sending** | Interrupted sends only resume the remaining bytes without resending what was already sent; brief back-off when the receiver is busy keeps latency realistic |
| **Integrity Check** | Validates file size on completion, discards incomplete files |
| **Transfer Cancellation** | Auto-cancels on file change and notifies receiver to clean up |
| **Host Offline** | All clients notified "Connection disconnected" |

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
- **Windows**: Download the installer from [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) or [Gitee Mirror](https://gitee.com/Lisselde_E/LANSyncBox/releases), then run it
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

## Feedback

**In Beta testing — if you have any questions or new ideas, feel free to contact me!**

Issues and Pull Requests are welcome!

## Support Me

If you find this tool useful, feel free to tip me on **ifdian (爱发电)** to support further development. Thank you so much for your kindness!

<a href="https://ifdian.net/a/lisseldee">
  <img src="https://img.shields.io/badge/ifdian-Support_me-018E96?style=for-the-badge" alt="Support me">
</a>
