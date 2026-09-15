<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-e1.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README.md) [![](https://img.shields.io/badge/-English-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/pro/README_EN.md)

</div>

> ⚠️ **Beta Notice**: This branch (`pro`) is the **Pro** edition of [**LANSyncBox**](https://github.com/LisseldeE/LANSyncBox/tree/main), under active development. Features are not yet fully stable and may change significantly. It is currently in the Beta testing stage — do not use it in production.

## Introduction

LANSyncBox Pro is the upgraded version of [LANSyncBox](https://github.com/LisseldeE/LANSyncBox/tree/main). It focuses on LAN multi-user collaboration. Compared to the standard edition, Pro is introducing several new capabilities — **Delivery** for cross-device copy & paste, top-edge **quick-add** by drag-and-drop, Collect mode, and more — under continuous development.

## Version Statement

The Pro edition will remain **open source** and will not become a paid product. Because this edition involves low-level changes, it may not be able to connect with the standard edition in future iterations. The Pro edition will be updated independently from the standard edition, and will not be merged into the main branch of the repository.

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

## Usage

- **Host (Create Connection)**: click "Create Connection" → optional password → create, entering the sync window
- **Client (Join Connection)**: click "Join Connection" → enter/select room code → pre-validate password if required → connect, auto full sync from host

## Tech Stack

- Python 3.x
- Qt6 (PySide6)
- Custom TCP Protocol

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