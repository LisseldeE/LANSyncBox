# LANSyncBox - LAN Real-time File Synchronization Tool

## Project Introduction

LANSyncBox is a lightweight LAN real-time file synchronization tool that enables multi-user file sharing. Connect within the same local network to share and sync files - no public network connection required. Built with the new Qt6 architecture, supports large file streaming transfers, optimized multi-connection sync logic, providing a smooth user experience.

## Project Information

- **Project Name**: LANSyncBox
- **Project Version**: R4
- **Project Author**: Lisselde_E
- **Contact Email**: Lisselde.E@outlook.com
- **Project Repository**: https://github.com/LisseldeE/LANSyncBox
- **Domestic Build Download Mirror**: https://gitee.com/Lisselde_E/LANSyncBox (Recommended for users in China)

## Features

### Real-time Sync
- File additions, modifications, deletions, and renames are synced to all connected clients instantly
- Support for custom 6-digit room codes for easy sharing
- Optional password verification for secure syncing
- Large file streaming transfers to avoid high memory usage
- Concurrent transfer limit (max 3 files simultaneously) to optimize system resource usage
- Initial full sync on first connection, automatically aligns differences between both ends

### Transfer Reliability
- Transfer cancellation: Auto-cancels when file changes during transfer, preventing file corruption
- Resumable transfer: Large file chunks are written by index positioning, failed retransmissions don't corrupt files
- Integrity check: Validates file size on completion, automatically discards incomplete files
- TCP buffer optimization: Increased send/receive buffers to avoid backpressure timeouts on large files
- Backpressure adaptation: Auto-retries on send timeout, distinguishes between cancellation and backpressure, ensuring stable large file transfers
- Auto-retransmit for failed clients: Skips and retransmits entire file when a client fails during broadcast, ensuring final sync

### Multi-client Sync
- Host monitors all changes in sync folder
- Changes synced to all connected clients in real-time
- Receives files from clients, forwards based on "hide flag"
- Support for folder sync, automatically recursively sync all files in folder
- Real-time transfer progress display with progress bar visualization

### Client Features
- File changes uploaded to host (not directly to other clients)
- "Hide files from others" toggle support
- Real-time sync status display
- Auto incremental sync after reconnection
- Real-time progress display with file transfer progress bar

### Multi-language Support
- Chinese/English interface switching
- Real-time language switching without restart
- Multi-language environment adaptation

### System Tray
- Minimized windows auto-hide to system tray
- Tray menu shows current room code
- Double-click tray icon to restore window
- Click "Exit" to close all syncs and quit

### UI Optimization
- New Qt6 architecture for smoother interface
- Optimized UI interaction logic for better user experience
- Real-time progress display with file transfer progress bar
- Optimized file list display logic

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

File conflicts are handled by the system file manager. Sync ensures consistency across all endpoints. Uses cache files and list operations to handle sync content, ensuring stable and reliable synchronization.

### Host Side

- Monitors all changes in sync folder (add/modify/delete/rename)
- Changes synced to all connected clients in real-time
- Receives files from clients, forwards based on "hide flag"
- Support for concurrent transfer limit (max 3 files simultaneously) to avoid high resource usage
- Support for folder sync, automatically recursively sync all files in folder
- Large files use streaming transfer to avoid high memory usage
- View all sync records and file sources
- View online client list

### Client Side

- File changes uploaded to host (not directly to other clients)
- "Hide files from others" toggle support
- Real-time sync status display
- Auto incremental sync after reconnection
- Real-time progress display with file transfer progress bar

### Additional Rules

- **Conflict Handling**: Latest modification time wins
- **Host Offline**: All clients notified "Room closed"
- **Transfer Protocol**: TCP + custom protocol
- **Large File Handling**: Streaming chunked transfer to avoid high memory usage
- **Concurrency Control**: Max 3 files transferred simultaneously to optimize system resource usage
- **Transfer Cancellation**: Auto-cancels transfer on file change, sends FILE_CANCEL to notify receiver cleanup
- **Resumable Transfer**: Chunks written by chunk_index positioning, failed retransmissions don't affect received parts
- **Integrity Check**: Validates file size on FILE_END, discards temp file if incomplete

## Change Log

### 2026.6.21 R4
**#01**
- Added initial full sync mechanism on first connection to align differences between both ends
- Added transfer cancellation mechanism to handle file corruption caused by file changes during transfer
- Improved transfer queue, fixed file skip error caused by queue conflicts
- Fixed blocking errors encountered during transfer cancellation
- Fixed numerous handle overflow and residual issues
- Optimized asynchronous processing logic during sync
- Fixed TCP buffer overflow issue

**#02**
- Fixed progress bar residual issue during sync cancellation
- Fixed UI handle overflow error caused by sync cancellation

### 2026.6.20 R3
**#01**
- Completely discarded previous versions, rebuilt the program
- New version uses cache files and list operations to handle sync content
- New Qt6 architecture
- Optimized many UI interaction logic, improved user experience
- Optimized UI element display logic
- Initial UI logic construction
- Sync logic not yet implemented

**#02**
- Added and optimized all sync logic
- Optimized encrypted room interaction logic
- Optimized multi-connection sync logic
- Fixed large file sync errors
- Fixed some known errors
- Optimized some text display content

### 2026.6.18 R1 (Deprecated)
**#01**
- Initial software build with sync functionality

### 2026.6.19 R2 (Deprecated)
**#01**
- Implemented multi-language switching, adapted to multi-language environments
- Optimized program update check logic
- Fixed some issues in sync logic
- Fixed abnormal delete signal error during file modification
- Optimized program startup speed, improved user experience

**#02**
- Refactored code logic, fixed some known errors
- Updated full sync to bidirectional sync
- Removed peer-to-peer sync between clients, now all clients stay consistent
- Added support for large file sync

## Tech Stack

- Python 3.x
- PySide6 (Qt6 GUI Framework)
- Custom TCP Protocol

## Installation & Running

### System Requirements
- Python 3.6 or higher
- Windows / macOS / Linux

### Install Dependencies
```bash
pip install PySide6
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