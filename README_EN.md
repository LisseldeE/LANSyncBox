<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-e.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/main/README.md) [![](https://img.shields.io/badge/-English-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/main/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/v/release/LisseldeE/LANSyncBox" alt="Latest Release"></a>
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/release-date/LisseldeE/LANSyncBox" alt="Release Date"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LisseldeE/LANSyncBox" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue?logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/platform-Linux-orange?logo=linux" alt="Platform">
</p>

<p align="center">
  <a href="#features">Features</a> |
  <a href="#system-support">Download</a> |
  <a href="#usage">Usage</a> |
  <a href="#sync-logic">Sync Logic</a> |
  <a href="#open-source-license">License</a>
</p>

## Project Introduction

LANSyncBox is a cross-platform file synchronization tool built specifically for LAN scenarios. It enables multiple users on the same local network to share and sync files safely and smoothly, with no public network connection required. Built with Qt6, it supports large-file streaming transfers, serialized sending, and resumable transfers to keep data reliable and transfers stable under multi-connection concurrency.

## Project Screenshots
| Main Interface | Sync Interface |
| :---: | :---: |
| ![Main Interface](https://lisseldee.github.io/assets/images/webp/1-1.webp) | ![Sync Interface](https://lisseldee.github.io/assets/images/webp/1-2.webp) |

## Project Information

- **Project Name**: LANSyncBox
- **Project Author**: Lisselde_E
- **License**: GNU General Public License v3.0
- **Project Homepage**: https://lisseldee.github.io/#1
- **Project Repository**: https://github.com/LisseldeE/LANSyncBox

## Features

| Area | Capability |
| :--- | :--- |
| **Real-time Sync** | File additions, edits, deletions, and renames sync to all clients instantly; initial connection auto-aligns differences |
| **Room Sharing** | Custom 6-digit room codes, optional password protection, version compatibility check when joining |
| **Large File Transfer** | Streaming chunked transfer, low memory usage; resumable, retransmits don't corrupt files |
| **Transfer Control** | Max 5 files transferred simultaneously; auto-cancels on change to prevent corruption |
| **Multi-client Sync** | Recursive folder sync; host forwards to all clients in real-time; incremental sync after manual reconnect |
| **File Operations** | Add, create, copy, cut, paste, delete, rename and other standard operations |
| **File Preview** | Double-click to preview files in read-only mode, preventing accidental edits |
| **Interface** | Smooth Qt6 UI, real-time Chinese/English switching, visible transfer progress bars |

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
  <a href="https://apps.microsoft.com/detail/9nsjvp7fxkm3?referrer=appbadge&mode=full">
    <img src="https://get.microsoft.com/images/en-us%20dark.svg" width="180" alt="Microsoft Store">
  </a>
</p>

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
| **Host** | Maintains file list, syncs changes to all clients in real-time; receives client files and forwards to others |
| **Client** | Uploads file changes to host (not directly to other clients); incremental sync after manual reconnect |
| **Conflict** | The version with the latest modification time wins |

### Sync Mechanisms

| Mechanism | Description |
| :--- | :--- |
| **Real-time Sync** | File changes recorded via operation list and dispatched in real-time |
| **Transfer Protocol** | TCP + custom protocol |
| **Streaming Transfer** | Chunked streaming to avoid loading entire files into memory |
| **Concurrency Control** | Max 5 files transferred simultaneously to optimize resource usage |
| **Resumable Transfer** | Chunks written by index positioning; retransmissions don't affect received parts |
| **Integrity Check** | Validates file size on completion, discards incomplete files |
| **Transfer Cancellation** | Auto-cancels on file change and notifies receiver to clean up |
| **Host Offline** | All clients notified "Connection disconnected" |

## Change Log

see [Changelog](https://github.com/LisseldeE/LANSyncBox/blob/main/CHANGELOG.md)

## Tech Stack

- Python 3.x
- Qt6 (PySide6)
- Custom TCP Protocol

## Installation & Running

### System Requirements
- Windows 10 or later (64-bit)
- Linux x64 (amd64) distributions

### Installation
- **Microsoft Store**: Search for LANSyncBox or click the download button above
- **Other Methods**: Download from [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) or [Gitee Mirror](https://gitee.com/Lisselde_E/LANSyncBox/releases), then run the installer
- **Linux (deb package)**: Download the architecture-appropriate `.deb` package from [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases), then install it from the terminal:
  ```bash
  sudo apt install -y ./lansyncbox_*.deb
  ```

### Running
- **Windows**: After installation, launch LANSyncBox from the Start menu or desktop shortcut
- **Linux**: Launch LANSyncBox from the app menu (Activities); if it reports missing system dependencies at runtime, run:
  ```bash
  sudo apt install -y libxcb-cursor0 libgl1 libxkbcommon-x11-0
  ```

## Open Source License

This project uses the GNU General Public License v3.0, see [LICENSE](https://github.com/LisseldeE/LANSyncBox/blob/main/LICENSE) file for details.

## Privacy Policy

This project does not collect any user data, see [Privacy Policy](https://github.com/LisseldeE/LANSyncBox/blob/main/privacy_policy.md) file for details.

## Feedback

**This application is under development, if you have any questions or new ideas, feel free to contact me!**

Issues and Pull Requests are welcome!