# LANSyncBox - LAN File Synchronization Tool

## Project Introduction

LANSyncBox is a lightweight LAN file synchronization tool that supports real-time file synchronization between multiple computers. Connect within the same local network to share and sync files - no public network connection required.

## Project Information

- **Project Name**: LANSyncBox
- **Project Version**: R2
- **Project Author**: Lisselde_E
- **Contact Email**: Lisselde.E@outlook.com
- **Project Repository**: https://github.com/LisseldeE/LANSyncBox
- **Domestic Build Download Mirror**: https://gitee.com/Lisselde_E/LANSyncBox (Recommended for users in China)

## Features

### Real-time Sync
- File additions, modifications, and deletions are synced to all connected clients instantly
- Support for custom 6-digit room codes for easy sharing
- Optional password verification for secure syncing

### Multi-client Sync
- Host monitors all changes in sync folder
- Changes synced to all connected clients in real-time
- Receives files from clients, forwards based on "hide flag"
- Maximum concurrent transfer limit (≤3 clients) to avoid high resource usage

### Client Features
- File changes uploaded to host (not directly to other clients)
- "Hide files from others" toggle support
- Real-time sync status display
- Auto incremental sync after reconnection

### Multi-language Support
- Chinese/English interface switching
- Real-time language switching without restart
- Multi-language environment adaptation

### System Tray
- Minimized windows auto-hide to system tray
- Tray menu shows current room code
- Double-click tray icon to restore window
- Click "Exit" to close all syncs and quit

## Usage

### Host (Create Connection)

1. Click "Create Connection" button
2. Enter a custom 6-digit room code (system checks availability)
3. Optional: Set password protection
4. Select the folder path to sync
5. Optional: Enable "Allow peer-to-peer sync"
6. Click create to enter sync status window

### Client (Join Connection)

1. Click "Join Connection" button
2. Enter room code
3. If password required, enter in popup dialog
4. Select folder path for synced files
5. Click connect - automatic full sync from host

## Sync Logic

### General Principle

File conflicts are handled by the system file manager. Sync ensures consistency across all endpoints.

### Host Side

- Monitors all changes in sync folder (add/modify/delete)
- Changes synced to all connected clients in real-time
- Receives files from clients, forwards based on "hide flag"
- Maximum concurrent transfer limit (≤3 clients) to avoid high resource usage
- View all sync records and file sources
- View online client list

### Client Side

- File changes uploaded to host (not directly to other clients)
- "Hide files from others" toggle support
- Real-time sync status display
- Auto incremental sync after reconnection

### Additional Rules

- **Conflict Handling**: Latest modification time wins
- **Host Offline**: All clients notified "Room closed"
- **Transfer Protocol**: TCP + custom protocol

## Change Log

### 2026.6.18 R1
**#01**
- Initial software build with sync functionality

### 2026.6.19 R2
**#01**
- Implemented multi-language switching, adapted to multi-language environments
- Optimized program update check logic
- Fixed some issues in sync logic
- Fixed abnormal delete signal error during file modification
- Optimized program startup speed, improved user experience

## Tech Stack

- Python 3.x
- PyQt5 (GUI Framework)
- watchdog (File Monitoring)

## Installation & Running

### System Requirements
- Python 3.6 or higher
- Windows / macOS / Linux

### Install Dependencies
```bash
pip install PyQt5 watchdog
```

### Run Program
```bash
python LANSyncBox.py
```

## Open Source License

This project uses the MIT open source license, see [LICENSE](LICENSE) file for details.

## Contact & Feedback

**This application is under development, if you have any questions or new ideas, feel free to contact me!**

- 📧 Email: Lisselde.E@outlook.com
- 🐙 GitHub: https://github.com/LisseldeE/LANSyncBox

Issues and Pull Requests are welcome!