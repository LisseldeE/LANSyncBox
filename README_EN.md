<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-e.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/main/README.md) [![](https://img.shields.io/badge/-English-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/main/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/v/release/LisseldeE/LANSyncBox" alt="Latest Release"></a>
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/release-date/LisseldeE/LANSyncBox" alt="Release Date"></a>
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/downloads/LisseldeE/LANSyncBox/total" alt="Total Downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LisseldeE/LANSyncBox" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue?logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/platform-Linux-orange?logo=linux" alt="Platform">
</p>

## Project Introduction

LANSyncBox is a simple and efficient LAN real-time file synchronization tool that enables multi-user file sharing. Connect within the same local network to share and sync files - no public network connection required. Built with the new Qt6 architecture, supports large file streaming transfers, optimized multi-connection sync logic, providing a smooth user experience.

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

## Features

### Real-time Sync
- File additions, modifications, deletions, and renames are synced to all connected clients instantly
- Support for custom 6-digit room codes for easy sharing
- Optional password verification for secure syncing
- Large file streaming transfers to avoid high memory usage
- Concurrent transfer limit (max 5 files simultaneously) to optimize system resource usage
- Initial full sync on first connection, automatically aligns differences between both ends
- Dual-end version verification when joining a room, ensuring connection compatibility

### Transfer Reliability
- Transfer cancellation: Auto-cancels when file changes during transfer, preventing file corruption
- Resumable transfer: Large file chunks are written by index positioning, failed retransmissions don't corrupt files
- Integrity check: Validates file size on completion, automatically discards incomplete files
- TCP buffer optimization: Increased send/receive buffers to avoid backpressure timeouts on large files
- Backpressure adaptation: Auto-retries on send timeout, distinguishes between cancellation and backpressure, ensuring stable large file transfers
- Auto-retransmit for failed clients: Skips and retransmits entire file when a client fails during broadcast, ensuring final sync

### Multi-client Sync
- Host file list changes synced to all connected clients in real-time
- Support for folder sync, automatically recursively sync all files in folder
- Real-time transfer progress display with progress bar visualization

### Client Features
- File changes uploaded to host (not directly to other clients)
- Real-time sync status display
- Auto incremental sync after reconnection
- Real-time progress display with file transfer progress bar

### File Operations
- Double-click empty list area to popup menu for quick file/folder addition
- Right-click menu supports adding files, adding folders, creating new folders
- New folder creation supports ESC cancellation during rename phase, avoiding accidental creation
- Supports common file operations: copy, cut, paste, delete, rename

### File Preview
- Double-click files to open in read-only preview mode
- Prevents accidental modifications during preview

### Cache Management
- Manage sync cache, clear local cache files

### Multi-language Support
- Chinese/English interface switching
- Real-time language switching without restart
- Multi-language environment adaptation

### UI Optimization
- New Qt6 architecture for smoother interface
- Optimized UI interaction logic for better user experience
- Real-time progress display with file transfer progress bar
- Optimized file list display logic

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

### General Principle

The core goal of sync is to keep file lists consistent across all endpoints. File changes are recorded via operation list, combined with cache file handling for transfers, ensuring stable and reliable synchronization. Conflict handling: latest modification time wins.

### Host Side

- Sync file changes via operation list
- Changes synced to all connected clients in real-time
- Receives client files, syncs to other clients in real-time
- Support for concurrent transfer limit (max 3 files simultaneously) to avoid high resource usage
- Support for folder sync, automatically recursively sync all files in folder
- Large files use streaming transfer to avoid high memory usage
- View all sync records and file sources
- View online client list

### Client Side

- File list changes uploaded to host (not directly to other clients)
- Real-time sync status display
- Auto sync and align after reconnection
- Real-time progress display with file transfer progress bar

### Additional Rules

- **Conflict Handling**: Latest modification time wins
- **Host Offline**: All clients notified "Connection disconnected"
- **Transfer Protocol**: TCP + custom protocol
- **Large File Handling**: Streaming chunked transfer to avoid high memory usage
- **Concurrency Control**: Max 5 files transferred simultaneously to optimize system resource usage
- **Transfer Cancellation**: Auto-cancels transfer on file change, sends FILE_CANCEL to notify receiver cleanup
- **Resumable Transfer**: Chunks written by chunk_index positioning, failed retransmissions don't affect received parts
- **Integrity Check**: Validates file size on FILE_END, discards temp file if incomplete

## Change Log

see [Changelog](https://github.com/LisseldeE/LANSyncBox/blob/main/CHANGELOG.md)

## Tech Stack

- Python 3.x
- PySide6
- Custom TCP Protocol

## Installation & Running

### System Requirements
- Windows 10 or later (64-bit)
- Ubuntu 22.04 LTS or later (64-bit)

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