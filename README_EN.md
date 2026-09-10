<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-e1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

> ⚠️ **Beta Notice**: This branch (`pro`) is the **Pro** edition of **LANSyncBox**, under active development. Features are not yet fully stable and may change significantly. It is currently in the Beta testing stage — do not use it in production.

## Introduction

LANSyncBox Pro is the upgraded version of [LANSyncBox](https://github.com/LisseldeE/LANSyncBox). It focuses on LAN multi-user collaboration, adding a host of new features to improve the user experience.

## Version Statement

The Pro edition will remain **open source** and will not become a paid product. Because this edition involves low-level changes, it may not be able to connect with the standard edition in future iterations.

## New Highlights (In Progress)

Compared to the old version, the Pro edition is adding the following:

| Feature | Description |
| :--- | :--- |
| **LAN Clipboard** | Active once a room is connected — one-click copy & paste of **text, images, and files** across devices on the LAN |
| **Distributed File Transfer (P2P)** | File metadata is distributed by the host, but pasting **pulls the file directly from the copying peer**, bypassing host forwarding |
| **Sync / Collect Modes** | New Collect mode: client files are submitted to the host only, without broadcasting to other clients |

> The above are planned Beta changes; specifics and progress may shift during development. Refer to actual releases.

## Core Features (Existing)

- **Real-time Sync**: additions, edits, deletions, and renames sync to all clients instantly; auto-aligns differences on first connect
- **Room Sharing**: custom 6-digit room codes, optional password protection, version compatibility check when joining
- **Large File Transfer**: streaming chunked transfer with resume support; transfers fail without corrupting files; up to 5 files at once, auto-cancels on change
- **File Operations**: add, create, copy, cut, paste, delete, rename; double-click for read-only preview
- **Interface**: smooth Qt6 UI, real-time Chinese/English switching, visible transfer progress

## Usage

- **Host (Create Connection)**: click "Create Connection" → optional password → create, entering the sync window
- **Client (Join Connection)**: click "Join Connection" → enter/select room code → pre-validate password if required → connect, auto full sync from host

## Tech Stack

- Python 3.x
- Qt6 (PySide6)
- Custom TCP Protocol

## Open Source License

This project uses the GNU General Public License v3.0, see [LICENSE](https://github.com/LisseldeE/LANSyncBox/blob/main/LICENSE) file for details.

## Privacy Policy

This project does not collect any user data, see [Privacy Policy](https://github.com/LisseldeE/LANSyncBox/blob/main/privacy_policy.md) file for details.

## Feedback

**In Beta testing — if you have any questions or new ideas, feel free to contact me!**

Issues and Pull Requests are welcome!