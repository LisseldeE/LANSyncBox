# LANSyncBox

LAN File Synchronization Tool

## Introduction

LANSyncBox is a lightweight LAN file synchronization tool that supports real-time file synchronization between multiple computers. Connect within the same local network to share and sync files - no public network connection required.

## Features

- **Real-time Sync**: File additions, modifications, and deletions are synced to all connected clients instantly
- **Custom Room Code**: Support for custom 6-digit room codes for easy sharing
- **Password Protection**: Optional password verification for secure syncing
- **Hide Mode**: "Hide files from others" option - files only sync to the host
- **Auto Reconnect**: Automatic connection detection and incremental sync after reconnection
- **System Tray**: Minimize to tray without interrupting daily work

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

## System Tray

- Minimized windows auto-hide to system tray
- Tray menu shows current room code
- Double-click tray icon to restore window
- Click "Exit" to close all syncs and quit

## Version Info

**Current Version: R1**

**Release Date: 2026.6.18**

**Author: Lisselde.E**

**Email: Lisselde.E@outlook.com**

## Change Log

### 2026.6.18 R1

- Initial software build with sync functionality

---

*For questions or suggestions, contact Lisselde.E@outlook.com*