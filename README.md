<p align="center">
  <img src="https://lisseldee.github.io/assets/images/webp/1-c.webp" width="100%" alt="LANSyncBox">
</p>

<div align="center">

[![](https://img.shields.io/badge/-简体中文-3b82f6?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/main/README.md) [![](https://img.shields.io/badge/-English-555555?style=flat)](https://github.com/LisseldeE/LANSyncBox/blob/main/README_EN.md)

</div>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/v/release/LisseldeE/LANSyncBox" alt="最新版本"></a>
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/release-date/LisseldeE/LANSyncBox" alt="发布时间"></a>
  <a href="https://github.com/LisseldeE/LANSyncBox/releases"><img src="https://img.shields.io/github/downloads/LisseldeE/LANSyncBox/total" alt="下载总量"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/LisseldeE/LANSyncBox" alt="开源协议"></a>
  <img src="https://img.shields.io/badge/platform-Windows-blue?logo=windows" alt="支持平台">
  <img src="https://img.shields.io/badge/platform-Linux-orange?logo=linux" alt="支持平台">
</p>

## 项目简介

LANSyncBox 是一款跨平台、专为局域网场景设计的文件实时同步工具，让多人只需在同一局域网内即可完成安全、流畅的文件共享与同步，全程无需公网。基于 Qt6 构建，支持大文件流式传输、串行化发送与断点续传，保证多连接高并发场景下的数据可靠与传输稳定。

## 项目截图
| 主界面 | 同步界面 |
| :---: | :---: |
| ![主界面](https://lisseldee.github.io/assets/images/webp/1-1.webp) | ![同步界面](https://lisseldee.github.io/assets/images/webp/1-2.webp) |

## 项目信息

- **项目名称**: LANSyncBox
- **项目作者**: Lisselde_E
- **开源协议**: GNU General Public License v3.0
- **项目主页**: https://lisseldee.github.io/#1
- **项目仓库**: https://github.com/LisseldeE/LANSyncBox

## 功能特性

| 维度 | 能力说明 |
| :--- | :--- |
| **实时同步** | 文件新增、修改、删除、重命名实时同步至所有连接端；初次连接自动全量对齐差异 |
| **房间分享** | 自定义 6 位数字房间号，可选密码验证，加入房间自动校验版本兼容性 |
| **大文件传输** | 流式分块传输，内存占用低；断点续传，失败重发不损坏文件 |
| **传输控制** | 最多同时传输 5 个文件；传输途中变化自动取消，避免文件损坏 |
| **多端同步** | 文件夹递归同步；主机实时转发至所有连接端；手动重连后自动增量补齐 |
| **文件操作** | 支持添加、新建、复制、剪切、粘贴、删除、重命名等常规操作 |
| **文件预览** | 双击文件只读预览，避免误修改内容 |
| **界面体验** | Qt6 流畅界面，中英文实时切换，传输进度条可视化 |

## 系统支持

<div align="center">

| 操作系统 | x64 (AMD64) | ARM64 |
| :--- | :---: | :---: |
| Windows | ✅ | ❌ |
| Linux | ✅ | ❌ |
| macOS | ❌ | ❌ |

</div>

> 目前仅覆盖 Windows 10/11 与 Linux (x64)；macOS 支持已在计划中

## 下载

<p align="center">
  <a href="https://apps.microsoft.com/detail/9nsjvp7fxkm3?referrer=appbadge&mode=full">
    <img src="https://get.microsoft.com/images/zh-cn%20dark.svg" width="180" alt="Microsoft Store">
  </a>
</p>

<p align="center">
  <a href="https://github.com/LisseldeE/LANSyncBox/releases">
    <img src="https://img.shields.io/badge/GitHub-Releases-181717?style=flat-square&logo=github&logoColor=white" alt="GitHub Releases">
  </a>
  &nbsp;&nbsp;
  <a href="https://gitee.com/Lisselde_E/LANSyncBox/releases">
    <img src="https://img.shields.io/badge/Gitee-镜像下载-C71D23?style=flat-square&logo=gitee&logoColor=white" alt="Gitee 镜像下载">
  </a>
</p>

> 💡 国内用户推荐使用 Gitee 镜像下载

## 使用方法

### 主机端（创建连接）

1. 点击"创建连接"按钮
2. 可选：设置密码保护
3. 点击创建，进入同步状态界面

### 连接端（加入连接）

1. 点击"加入连接"按钮
2. 输入或选择房间号
3. 如需密码，在加入界面直接输入并预验证
4. 验证失败时直接在加入界面提示，可即时修改信息重试
5. 点击连接，自动从主机全量同步一次

## 同步逻辑

### 端职责

| 端 | 职责 |
| :--- | :--- |
| **主机端** | 维护文件列表，实时同步变更至所有连接端；接收连接端文件并转发至其他端 |
| **连接端** | 文件变更上传至主机端（不直接发给其他连接端）；手动重连后自动增量补齐 |
| **冲突处理** | 以最后修改时间较新的版本为准 |

### 同步机制

| 机制 | 说明 |
| :--- | :--- |
| **实时同步** | 通过操作列表记录并实时派发文件变更 |
| **传输协议** | TCP + 自定义协议 |
| **大文件流式传输** | 分块流式传输，避免整文件载入内存 |
| **并发控制** | 最多同时传输 5 个文件，优化资源占用 |
| **断点续传** | 分块按索引定位写入，失败重发不影响已接收部分 |
| **完整性校验** | 接收完成时校验文件大小，不完整自动丢弃 |
| **传输取消** | 传输途中文件变化自动取消，并通知接收端清理 |
| **主机离线** | 向所有连接端提示「连接已断开」 |

## 更新日志

详见 [更新日志](https://github.com/LisseldeE/LANSyncBox/blob/main/CHANGELOG.md)

## 技术栈

- Python 3.x
- Qt6 (PySide6)
- 自定义TCP协议

## 安装与运行

### 系统要求
- Windows 10 或更高版本（64位）
- Linux x64（amd64）发行版

### 安装方式
- **Microsoft Store**：搜索 LANSyncBox 或点击上方下载按钮安装
- **其他方式**：从 [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) 或 [Gitee 镜像](https://gitee.com/Lisselde_E/LANSyncBox/releases) 下载安装包，双击运行即可
- **Linux（deb 包）**：从 [GitHub Releases](https://github.com/LisseldeE/LANSyncBox/releases) 下载对应架构的 `.deb` 安装包，在终端中执行以下命令安装：
  ```bash
  sudo apt install -y ./lansyncbox_*.deb
  ```

### 运行
- **Windows**：安装完成后，从开始菜单或桌面快捷方式启动 LANSyncBox
- **Linux**：从应用菜单（Activities）中搜索 LANSyncBox 并启动；若运行时提示缺少系统依赖，请先执行：
  ```bash
  sudo apt install -y libxcb-cursor0 libgl1 libxkbcommon-x11-0
  ```

## 开源声明

本项目采用 GNU General Public License v3.0 开源协议，详见 [LICENSE](https://github.com/LisseldeE/LANSyncBox/blob/main/LICENSE) 文件。

## 隐私政策

本项目不收集任何用户数据，详见 [隐私政策](https://github.com/LisseldeE/LANSyncBox/blob/main/privacy_policy.md) 文件。

## 反馈

**开发中应用，如有问题或新的创意欢迎和我联系！**

欢迎提交 Issue 和 Pull Request！
