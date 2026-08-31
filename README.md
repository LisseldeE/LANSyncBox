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

LANSyncBox 是一款简洁高效的局域网文件实时同步工具，实现多人隔空文件共享。在同一局域网内即可实现文件共享与同步，无需公网连接。采用全新QT6架构，支持大文件流式传输，优化多连接同步逻辑，提供流畅的用户体验。

## 系统支持

<div align="center">

| 操作系统 | x64 (AMD64) | ARM64 |
| :--- | :---: | :---: |
| Windows 10 / 11 | ✅ | ❌ |
| Linux (Ubuntu 等) | ✅ | ❌ |
| macOS | ❌ | ❌ |

</div>

> 目前仅覆盖 Windows 10/11 与 Linux (x64)；macOS 我争取后续支持

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

## 功能特性

### 实时同步
- 列表中文件添加、修改、删除、重命名操作实时同步至所有连接端
- 支持自定义6位数字房间号，方便记忆与分享
- 可选密码验证，确保同步安全
- 大文件流式传输，避免内存占用过高
- 并发传输限制（最多同时传输5个文件），优化系统资源占用
- 初次连接全量同步，自动对齐双端差异
- 加入房间双端版本号验证，确保连接兼容性

### 传输可靠性
- 传输取消机制：传输中途文件变化时自动取消，避免文件损坏
- 断点续传：大文件分块按索引定位写入，失败重发不损坏文件
- 完整性校验：文件接收完成时校验大小，不完整自动丢弃
- TCP 缓冲区优化：增大收发缓冲区，避免大文件背压超时
- 背压自适应：发送超时自动重试，区分取消与背压，确保大文件稳定传输
- 失败客户端自动重发：广播中某客户端失败时跳过并重发整个文件，确保最终同步

### 多端同步
- 主机端文件列表修改实时同步至所有连接端
- 支持文件夹同步，自动递归同步文件夹内所有文件
- 实时显示传输进度，支持进度条可视化

### 连接端功能
- 文件变更上传至主机端，由主机端转发至其他端
- 实时显示同步状态
- 断线重连后自动增量同步
- 实时进度显示，支持文件传输进度条

### 文件操作
- 双击列表空白区域弹出菜单，快速添加文件/文件夹
- 右键菜单支持添加文件、添加文件夹、新建文件夹
- 新建文件夹支持重命名阶段 ESC 取消，避免误创建
- 支持复制、剪切、粘贴、删除、重命名等常规文件操作

### 文件预览
- 支持双击文件直接预览，预览窗口为只读模式
- 避免预览时误修改文件内容

### 缓存管理
- 支持管理同步缓存，清理本地缓存文件

### 多语言支持
- 支持中文/英文界面切换
- 实时切换语言无需重启程序
- 适配多语言环境

### 界面优化
- 全新QT6架构，界面更流畅
- 优化的界面交互逻辑，提升使用体验
- 实时进度显示，支持文件传输进度条
- 优化的文件列表显示逻辑

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

### 总体原则

同步的核心目标是保持所有端的文件列表一致。通过操作列表记录文件变更，配合缓存文件处理传输，确保同步过程稳定可靠。冲突处理以最后修改时间较新的为准。

### 主机端

- 通过操作列表同步文件变更
- 变更实时同步至所有连接端
- 接收连接端文件，实时同步至其他连接端
- 支持并发传输限制（最多同时传输3个文件），避免资源占用过高
- 支持文件夹同步，自动递归同步文件夹内所有文件
- 大文件采用流式传输，避免内存占用过高
- 可查看所有同步记录及文件来源
- 可查看在线连接端列表

### 连接端

- 文件列表变更上传至主机端（不直接发给其他连接端）
- 实时显示同步状态
- 断线重连后自动同步补齐
- 实时进度显示，支持文件传输进度条

### 补充规则

- **冲突处理**：以最后修改时间较新的为准
- **主机离线**：所有连接端提示"连接已断开"
- **传输协议**：TCP + 自定义协议
- **大文件处理**：采用流式分块传输，避免内存占用过高
- **并发控制**：最多同时传输5个文件，优化系统资源占用
- **传输取消**：文件变化时自动取消传输，发送 FILE_CANCEL 通知接收端清理
- **断点续传**：分块按 chunk_index 定位写入，失败重发不影响已接收部分
- **完整性校验**：FILE_END 时校验文件大小，不完整则丢弃临时文件

## 更新日志

详见 [更新日志](https://github.com/LisseldeE/LANSyncBox/blob/main/CHANGELOG.md)

## 技术栈

- Python 3.x
- PySide6
- 自定义TCP协议

## 安装与运行

### 系统要求
- Windows 10 或更高版本（64位）
- Ubuntu 22.04 LTS 或更高版本（64位）

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
