"""
文件列表组件
支持拖拽、右键菜单、文件操作
所有同步信号由 UI 操作触发，不使用文件监听
"""
import os
import threading
import shutil
from pathlib import Path
from typing import List, Optional, Set
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, 
    QTableWidgetItem, QHeaderView, QMenu, QMessageBox, 
    QFileDialog, QAbstractItemView, QLabel, QPushButton, QLineEdit
)
from PySide6.QtCore import Qt, Signal, QMimeData, QUrl, QPoint, QThread, QMetaObject, Q_ARG
from PySide6.QtGui import QAction, QIcon, QDrag, QDropEvent, QDragEnterEvent, QDragMoveEvent, QCursor
from PySide6.QtWidgets import QApplication

from i18n import I18n
from config import Config
from ui.widgets import BUTTON_STYLES


class DragableTableWidget(QTableWidget):
    """支持拖拽的表格控件"""

    files_dragged = Signal(list, str, bool)  # 文件拖拽信号（文件列表，目标路径，是否内部拖拽）
    empty_area_double_clicked = Signal()  # 空白区域双击信号（用于触发添加文件）

    def __init__(self, parent=None):
        super().__init__(parent)
        # 不启用默认的拖拽功能，我们手动控制
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        
        # 记录拖拽的文件，用于判断是否是拖拽到外部
        self._drag_files = []
        
        # 记录鼠标按下时的状态，用于区分框选和拖拽
        self._mouse_press_pos = None
        self._mouse_press_item = None
        self._is_dragging = False
    
    def mousePressEvent(self, event):
        """鼠标按下事件"""
        if event.button() == Qt.LeftButton:
            self._mouse_press_pos = event.pos()
            self._mouse_press_item = self.itemAt(event.pos())
            self._is_dragging = False
        super().mousePressEvent(event)
    
    def mouseMoveEvent(self, event):
        """鼠标移动事件"""
        if event.buttons() & Qt.LeftButton and self._mouse_press_pos:
            # 计算移动距离
            distance = (event.pos() - self._mouse_press_pos).manhattanLength()
            
            # 如果移动距离超过阈值，且鼠标在选中的项目上，则开始拖拽
            if distance > 10 and not self._is_dragging:
                # 检查鼠标按下时是否在选中的项目上
                if self._mouse_press_item and self._mouse_press_item.isSelected():
                    self._is_dragging = True
                    # 开始拖拽
                    self._start_drag()
                    return
        
        # 否则调用父类的鼠标移动事件（支持框选）
        super().mouseMoveEvent(event)
    
    def mouseReleaseEvent(self, event):
        """鼠标释放事件"""
        self._mouse_press_pos = None
        self._mouse_press_item = None
        self._is_dragging = False
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        """鼠标双击事件

        双击空白区域（无单元格）时发射 empty_area_double_clicked 信号，
        用于触发"添加文件"操作；双击单元格时交给父类处理（触发 cellDoubleClicked）。
        """
        if event.button() == Qt.LeftButton:
            # itemAt 返回 None 表示双击在空白区域
            if self.itemAt(event.pos()) is None:
                self.empty_area_double_clicked.emit()
                return
        super().mouseDoubleClickEvent(event)

    def _start_drag(self):
        """开始拖拽"""
        # 获取选中的文件
        files = []
        for item in self.selectedItems():
            if item.column() == 0:  # 只处理文件名列
                path = item.data(Qt.UserRole)
                if path:
                    files.append(path)
        
        if not files:
            return
        
        # 记录拖拽的文件
        self._drag_files = files
        
        # 创建拖拽对象
        drag = QDrag(self)
        mime_data = QMimeData()
        
        # 设置文件URL
        urls = [QUrl.fromLocalFile(str(f)) for f in files]
        mime_data.setUrls(urls)
        drag.setMimeData(mime_data)
        
        # 执行拖拽（只支持复制操作）
        drag.exec(Qt.CopyAction)
        
        # 清空记录
        self._drag_files = []
    
    def dragEnterEvent(self, event):
        """拖拽进入事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dragMoveEvent(self, event):
        """拖拽移动事件"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def dropEvent(self, event):
        """拖拽放下事件"""
        if event.mimeData().hasUrls():
            # 获取拖拽的文件
            urls = event.mimeData().urls()
            files = [url.toLocalFile() for url in urls]
            
            # 获取目标位置
            target_path = self._get_drop_target(event.pos())
            
            # 检查是否是内部拖拽
            is_internal = event.source() == self
            
            # 发送信号，让父控件处理
            self.files_dragged.emit(files, target_path, is_internal)
            event.acceptProposedAction()
        else:
            event.ignore()
    
    def _get_drop_target(self, pos):
        """获取拖拽目标路径"""
        # 获取鼠标位置对应的单元格
        item = self.itemAt(pos)
        if item:
            # 获取该行的文件路径
            row = item.row()
            name_item = self.item(row, 0)
            if name_item:
                path = name_item.data(Qt.UserRole)
                if path:
                    from pathlib import Path
                    path = Path(path)
                    # 如果是文件夹，返回该文件夹路径
                    if path.is_dir():
                        return str(path)
        
        # 如果没有目标或目标不是文件夹，返回空字符串（表示当前目录）
        return ""


class FileCopyWorker(QThread):
    """文件复制工作线程"""
    
    # 信号
    progress_updated = Signal(int, int, int)  # 当前文件索引, 当前进度, 总进度
    file_started = Signal(str)  # 开始复制文件
    file_finished = Signal(str)  # 文件复制完成
    all_finished = Signal()  # 所有文件复制完成
    error_occurred = Signal(str)  # 发生错误
    
    def __init__(self, file_paths: List[str], dest_path: Path):
        super().__init__()
        self.file_paths = file_paths
        self.dest_path = dest_path
        self._is_cancelled = False
    
    def run(self):
        """执行文件复制"""
        try:
            for idx, src_path in enumerate(self.file_paths):
                if self._is_cancelled:
                    break
                
                src = Path(src_path)
                if not src.exists():
                    continue
                
                dst = self.dest_path / src.name
                
                # 发送开始信号
                self.file_started.emit(src.name)
                
                try:
                    if src.is_dir():
                        # 复制文件夹（如果目标存在，先删除再复制）
                        if dst.exists():
                            from sync.file_manager import safe_rmtree
                            safe_rmtree(dst)
                        shutil.copytree(src, dst)
                    else:
                        # 复制文件（分块复制以显示进度）
                        self._copy_file_with_progress(src, dst, idx)
                    
                    # 发送完成信号
                    self.file_finished.emit(str(dst))
                    
                except Exception as e:
                    self.error_occurred.emit(f"复制 {src.name} 失败: {str(e)}")
            
            # 发送完成信号
            self.all_finished.emit()
            
        except Exception as e:
            self.error_occurred.emit(f"复制过程出错: {str(e)}")
    
    def _copy_file_with_progress(self, src: Path, dst: Path, file_idx: int):
        """分块复制文件并更新进度"""
        file_size = src.stat().st_size
        chunk_size = 1024 * 1024  # 1MB
        copied = 0
        last_progress = -1
        
        with open(src, 'rb') as f_src, open(dst, 'wb') as f_dst:
            while copied < file_size:
                if self._is_cancelled:
                    break
                
                chunk = f_src.read(chunk_size)
                if not chunk:
                    break
                
                f_dst.write(chunk)
                copied += len(chunk)
                
                # 计算进度百分比
                progress = int(copied / file_size * 100)
                
                # 只在进度变化超过1%时更新
                if progress != last_progress:
                    self.progress_updated.emit(file_idx, progress, 0)
                    last_progress = progress
        
        # 复制元数据
        shutil.copystat(src, dst)
    
    def cancel(self):
        """取消复制"""
        self._is_cancelled = True


class FileListWidget(QWidget):
    """文件列表组件"""

    # 信号 - 所有同步信号由 UI 操作触发
    file_added = Signal(str)  # 文件添加信号（本地操作触发）
    file_deleted = Signal(str)  # 文件删除信号（本地操作触发）
    file_renamed = Signal(str, str)  # 文件重命名信号（旧名，新名）
    dir_created = Signal(str)  # 目录创建信号（本地操作触发）

    def __init__(self, folder_path: Path, parent=None):
        super().__init__(parent)
        self.folder_path = folder_path
        self.current_path = folder_path

        # 上次文件对话框使用的目录（实例变量，重启程序后自动重置）
        self._last_dialog_dir: Optional[str] = None

        # 剪贴板
        self.clipboard_files: List[Path] = []
        self.clipboard_is_cut = False
        
        # 同步文件集合（正在同步的文件，用于避免循环同步）
        self._syncing_files: Set[str] = set()
        self._syncing_lock = threading.Lock()
        
        # 标记是否正在接收远程文件（刷新文件列表时不触发同步）
        self._receiving_remote = False
        
        # 取消传输回调（由 SyncWindow 设置，直接调用避免 Qt 信号异步性问题）
        self._cancel_transfer_callback = None
        
        self.init_ui()
        self.load_files()
    
    def set_cancel_transfer_callback(self, callback):
        """设置取消传输回调函数
        
        Args:
            callback: 回调函数，签名为 callback(rel_path: str)
        """
        self._cancel_transfer_callback = callback
    
    # ========== 同步标记方法 ==========
    
    def mark_syncing(self, path: str):
        """
        标记文件正在同步（发送或接收）
        用于区分本地操作和远程写入
        
        Args:
            path: 文件路径（相对路径或绝对路径）
        """
        normalized = str(path).replace('\\', '/')
        with self._syncing_lock:
            self._syncing_files.add(normalized)
    
    def unmark_syncing(self, path: str):
        """
        取消标记文件正在同步
        
        Args:
            path: 文件路径
        """
        normalized = str(path).replace('\\', '/')
        with self._syncing_lock:
            self._syncing_files.discard(normalized)
    
    def is_syncing(self, path: str) -> bool:
        """
        检查文件是否正在同步
        
        Args:
            path: 文件路径
        Returns:
            True: 正在同步，不应触发同步信号
            False: 本地操作，应触发同步信号
        """
        normalized = str(path).replace('\\', '/')
        with self._syncing_lock:
            return normalized in self._syncing_files
    
    def set_receiving_remote(self, receiving: bool):
        """
        设置是否正在接收远程文件
        接收远程文件时刷新列表不会触发同步信号
        
        Args:
            receiving: 是否正在接收
        """
        self._receiving_remote = receiving
    
    def init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        
        # 工具栏
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(5, 5, 5, 5)
        
        # 返回上级按钮
        self.back_btn = QPushButton(I18n.tr('go_up'))
        self.back_btn.setFixedWidth(80)
        self.back_btn.clicked.connect(self.go_back)
        toolbar_layout.addWidget(self.back_btn)
        
        # 当前路径
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        toolbar_layout.addWidget(self.path_edit)
        
        # 刷新按钮
        refresh_btn = QPushButton(I18n.tr('refresh'))
        refresh_btn.setFixedWidth(60)
        refresh_btn.clicked.connect(self.load_files)
        toolbar_layout.addWidget(refresh_btn)
        
        layout.addWidget(toolbar)
        
        # 文件列表表格
        self.table = DragableTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels([
            I18n.tr('file_name'),
            I18n.tr('file_size'),
            I18n.tr('file_modified'),
            I18n.tr('file_status')
        ])
        
        # 禁用编辑（避免双击时进入编辑模式）
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        
        # 设置表头
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        
        # 连接拖拽信号
        self.table.files_dragged.connect(self._handle_files_dragged)

        # 双击事件
        self.table.cellDoubleClicked.connect(self.on_double_click)
        # 双击空白区域 → 在鼠标位置弹出添加文件/文件夹菜单
        self.table.empty_area_double_clicked.connect(self.on_add_via_double_click)
        
        # 右键菜单
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        
        layout.addWidget(self.table)
        
        # 拖拽提示
        drag_hint = QLabel(I18n.tr('drag_files_hint'))
        drag_hint.setAlignment(Qt.AlignCenter)
        drag_hint.setStyleSheet("color: #868e96; font-size: 12px; padding: 5px;")
        layout.addWidget(drag_hint)
        
        # 更新路径显示
        self.update_path_display()
    
    def load_files(self):
        """加载文件列表"""
        self.table.setRowCount(0)
        
        if not self.current_path.exists():
            return
        
        # 获取文件列表
        items = list(self.current_path.iterdir())
        
        # 排序：文件夹在前，然后按名称排序
        items.sort(key=lambda x: (not x.is_dir(), x.name.lower()))
        
        # 添加文件到表格
        for item in items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            
            # 文件名
            name_item = QTableWidgetItem(item.name)
            if item.is_dir():
                name_item.setIcon(QIcon.fromTheme("folder"))
            else:
                name_item.setIcon(QIcon.fromTheme("text-x-generic"))
            self.table.setItem(row, 0, name_item)
            
            # 文件大小
            if item.is_dir():
                size_text = "<DIR>"
            else:
                size = item.stat().st_size
                size_text = self.format_size(size)
            size_item = QTableWidgetItem(size_text)
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 1, size_item)
            
            # 修改时间
            mtime = datetime.fromtimestamp(item.stat().st_mtime)
            mtime_text = mtime.strftime("%Y-%m-%d %H:%M:%S")
            mtime_item = QTableWidgetItem(mtime_text)
            self.table.setItem(row, 2, mtime_item)
            
            # 同步状态
            status_item = QTableWidgetItem(I18n.tr('status_synced'))
            status_item.setForeground(Qt.green)
            self.table.setItem(row, 3, status_item)
            
            # 保存路径信息
            name_item.setData(Qt.UserRole, str(item))
    
    def format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def update_path_display(self):
        """更新路径显示"""
        try:
            relative_path = self.current_path.relative_to(self.folder_path)
            self.path_edit.setText(str(relative_path) if str(relative_path) != "." else "/")
        except ValueError:
            self.path_edit.setText(str(self.current_path))
        
        # 更新返回按钮状态
        self.back_btn.setEnabled(self.current_path != self.folder_path)
    
    def go_back(self):
        """返回上级目录"""
        if self.current_path != self.folder_path:
            self.current_path = self.current_path.parent
            self.load_files()
            self.update_path_display()
    
    def on_double_click(self, row: int, column: int):
        """双击事件"""
        item = self.table.item(row, 0)
        if not item:
            return
        
        path = Path(item.data(Qt.UserRole))
        
        if path.is_dir():
            # 进入文件夹
            self.current_path = path
            self.load_files()
            self.update_path_display()
        else:
            # 双击文件：复制到预览目录后打开（只读预览模式）
            try:
                # 获取预览目录（不按房间号分隔）
                preview_folder = Config.get_preview_folder()
                
                # 复制文件到预览目录（保持相对路径结构）
                relative_path = path.relative_to(self.folder_path)
                preview_path = preview_folder / relative_path
                
                # 确保目标目录存在
                preview_path.parent.mkdir(parents=True, exist_ok=True)

                # 如果预览文件已存在，移除只读属性（让shutil.copy2可覆盖）
                if preview_path.exists():
                    try:
                        import ctypes
                        # 移除只读属性（FILE_ATTRIBUTE_NORMAL = 0x80）
                        ctypes.windll.kernel32.SetFileAttributesW(str(preview_path), 0x80)
                    except Exception:
                        # 移除属性失败（如权限问题），让shutil.copy2自然抛出PermissionError
                        # 外层try/except会捕获并显示错误消息
                        pass

                # 复制文件（覆盖旧的预览文件）
                # shutil.copy2会直接打开文件写入（不先删除）：
                # - 普通文件：成功覆盖（Windows允许写入已占用文件）
                # - 只读文件：抛出PermissionError（外层捕获）
                shutil.copy2(path, preview_path)
                
                # 设置只读属性（Windows API）
                import ctypes
                # FILE_ATTRIBUTE_READONLY = 0x1
                ctypes.windll.kernel32.SetFileAttributesW(str(preview_path), 0x1)
                
                # 用系统默认应用打开预览副本
                os.startfile(str(preview_path))
                
            except Exception as e:
                QMessageBox.warning(
                    self,
                    I18n.tr('file'),
                    f"{I18n.tr('tip_open_file')}\n\n错误: {e}"
                )
    
    def show_context_menu(self, pos: QPoint):
        """显示右键菜单"""
        menu = QMenu(self)

        # 添加文件
        add_file_action = QAction(I18n.tr('drag_add'), self)
        add_file_action.triggered.connect(self.on_add_files)
        menu.addAction(add_file_action)

        # 添加文件夹
        add_folder_action = QAction(I18n.tr('add_folder'), self)
        add_folder_action.triggered.connect(self.on_add_folder)
        menu.addAction(add_folder_action)

        menu.addSeparator()

        # 新建文件夹
        new_folder_action = QAction(I18n.tr('new_folder'), self)
        new_folder_action.triggered.connect(self.on_new_folder)
        menu.addAction(new_folder_action)

        menu.addSeparator()

        # 是否有选中的文件（用于控制复制/剪切/删除/重命名的启用状态）
        has_selection = len(self.get_selected_files()) > 0

        # 复制
        copy_action = QAction(I18n.tr('copy'), self)
        copy_action.triggered.connect(self.copy_files)
        copy_action.setEnabled(has_selection)
        menu.addAction(copy_action)

        # 剪切
        cut_action = QAction(I18n.tr('cut'), self)
        cut_action.triggered.connect(self.cut_files)
        cut_action.setEnabled(has_selection)
        menu.addAction(cut_action)

        # 粘贴
        paste_action = QAction(I18n.tr('paste'), self)
        paste_action.triggered.connect(self.paste_files)
        paste_action.setEnabled(len(self.clipboard_files) > 0)
        menu.addAction(paste_action)

        menu.addSeparator()

        # 删除
        delete_action = QAction(I18n.tr('delete'), self)
        delete_action.triggered.connect(self.delete_files)
        delete_action.setEnabled(has_selection)
        menu.addAction(delete_action)

        # 重命名 - 检查选中的行数
        rename_action = QAction(I18n.tr('rename'), self)
        rename_action.triggered.connect(self.rename_file)
        selected_rows = self.table.selectionModel().selectedRows()
        rename_action.setEnabled(len(selected_rows) == 1)
        menu.addAction(rename_action)
        
        menu.exec(self.table.viewport().mapToGlobal(pos))
    
    def get_selected_files(self) -> List[Path]:
        """获取选中的文件列表"""
        files = []
        for item in self.table.selectedItems():
            if item.column() == 0:  # 只处理文件名列
                path = Path(item.data(Qt.UserRole))
                files.append(path)
        return files
    
    def copy_files(self):
        """复制文件"""
        self.clipboard_files = self.get_selected_files()
        self.clipboard_is_cut = False
    
    def cut_files(self):
        """剪切文件"""
        self.clipboard_files = self.get_selected_files()
        self.clipboard_is_cut = True
    
    def paste_files(self):
        """粘贴文件"""
        if not self.clipboard_files:
            return
        
        for src_file in self.clipboard_files:
            if not src_file.exists():
                continue
            
            dst_file = self.current_path / src_file.name
            
            # 检查文件是否存在
            if dst_file.exists():
                # 创建自定义消息框
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle(I18n.tr('confirm_replace'))
                msg_box.setText(I18n.tr('confirm_replace_msg'))
                msg_box.setIcon(QMessageBox.Question)
                
                # 添加自定义按钮
                yes_btn = msg_box.addButton(I18n.tr('yes'), QMessageBox.YesRole)
                no_btn = msg_box.addButton(I18n.tr('no'), QMessageBox.NoRole)
                
                # 应用全局按钮样式
                yes_btn.setStyleSheet(BUTTON_STYLES['primary'])
                no_btn.setStyleSheet(BUTTON_STYLES['secondary'])
                
                msg_box.setDefaultButton(no_btn)
                msg_box.exec()
                
                if msg_box.clickedButton() == no_btn:
                    continue
            
            try:
                if self.clipboard_is_cut:
                    # 移动文件
                    src_file.rename(dst_file)
                    # 只有本地操作才触发同步信号
                    if not self.is_syncing(str(src_file)):
                        self.file_renamed.emit(str(src_file), str(dst_file))
                else:
                    # 复制文件
                    import shutil
                    if src_file.is_dir():
                        shutil.copytree(src_file, dst_file)
                    else:
                        shutil.copy2(src_file, dst_file)
                    # 只有本地操作才触发同步信号
                    if not self.is_syncing(str(dst_file)):
                        self.file_added.emit(str(dst_file))
            except Exception as e:
                # 创建自定义错误框
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("错误")
                msg_box.setText(f"粘贴失败: {str(e)}")
                msg_box.setIcon(QMessageBox.Critical)
                
                # 添加自定义按钮
                ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
                ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
                
                msg_box.exec()
        
        # 清空剪贴板（如果是剪切）
        if self.clipboard_is_cut:
            self.clipboard_files = []
            self.clipboard_is_cut = False
        
        self.load_files()
    
    def delete_files(self):
        """删除文件"""
        files = self.get_selected_files()
        if not files:
            return
        
        # 创建自定义消息框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(I18n.tr('confirm_delete'))
        msg_box.setText(I18n.tr('confirm_delete_msg'))
        msg_box.setIcon(QMessageBox.Question)
        
        # 添加自定义按钮
        yes_btn = msg_box.addButton(I18n.tr('yes'), QMessageBox.YesRole)
        no_btn = msg_box.addButton(I18n.tr('no'), QMessageBox.NoRole)
        
        # 应用全局按钮样式
        yes_btn.setStyleSheet(BUTTON_STYLES['danger'])
        no_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        
        msg_box.setDefaultButton(no_btn)
        msg_box.exec()
        
        if msg_box.clickedButton() == yes_btn:
            import shutil
            import time
            for file in files:
                try:
                    # 删除前先取消该文件的传输任务，避免 Windows 文件锁导致删除失败
                    try:
                        rel_path = str(file.relative_to(self.folder_path)).replace('\\', '/')
                        # 直接调用回调（同步执行），避免 Qt 信号异步性导致主线程阻塞时信号无法处理
                        if self._cancel_transfer_callback:
                            self._cancel_transfer_callback(rel_path)
                    except Exception:
                        pass
                    
                    # 循环尝试删除，覆盖发送任务退出期间的最坏情况：
                    # sendall(chunk) 超时 1 秒 + sendall(FILE_CANCEL) 超时 1 秒 = 2 秒
                    # 固定 sleep 无法保证覆盖，改用循环重试，最多 3 秒
                    from sync.file_manager import safe_rmtree
                    deleted = False
                    last_error = None
                    for attempt in range(30):
                        try:
                            if file.is_dir():
                                safe_rmtree(file)
                            else:
                                file.unlink()
                            deleted = True
                            break
                        except PermissionError as pe:
                            last_error = pe
                            time.sleep(0.1)
                        except OSError as oe:
                            last_error = oe
                            time.sleep(0.1)

                    if not deleted:
                        raise last_error if last_error else Exception("删除失败")
                    
                    # 只有本地操作才触发同步信号
                    if not self.is_syncing(str(file)):
                        self.file_deleted.emit(str(file))
                except Exception as e:
                    # 创建自定义错误框
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("错误")
                    msg_box.setText(f"删除失败: {str(e)}")
                    msg_box.setIcon(QMessageBox.Critical)
                    
                    # 添加自定义按钮
                    ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
                    ok_btn.setStyleSheet(BUTTON_STYLES['primary'])

                    msg_box.exec()

            self.load_files()

    def on_add_via_double_click(self):
        """双击空白区域：在鼠标位置弹出"添加文件/文件夹"选择菜单"""
        menu = QMenu(self)
        file_action = menu.addAction(I18n.tr('drag_add'))
        folder_action = menu.addAction(I18n.tr('add_folder'))

        # 在鼠标光标位置显示菜单
        chosen = menu.exec(QCursor.pos())

        if chosen == file_action:
            self.on_add_files()
        elif chosen == folder_action:
            self.on_add_folder()

    def _get_dialog_start_dir(self) -> str:
        """获取文件对话框的起始目录：优先使用上次记忆的目录，否则用当前路径"""
        return self._last_dialog_dir or str(self.current_path)

    def on_add_files(self):
        """添加文件：打开系统文件选择对话框（支持多选），调用 add_files 同步"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            I18n.tr('drag_add'),
            self._get_dialog_start_dir()
        )
        if files:
            # 记忆本次选择的目录，下次打开时定位到此
            self._last_dialog_dir = str(Path(files[0]).parent)
            self.add_files(files)

    def on_add_folder(self):
        """添加文件夹：打开系统文件夹选择对话框，调用 add_files 同步"""
        folder = QFileDialog.getExistingDirectory(
            self,
            I18n.tr('add_folder'),
            self._get_dialog_start_dir()
        )
        if folder:
            # 记忆本次选择的目录，下次打开时定位到此
            self._last_dialog_dir = folder
            self.add_files([folder])

    def on_new_folder(self):
        """新建文件夹

        流程：弹出重命名对话框（预填默认名）→ 确认名称后正式创建并触发同步；
        对话框阶段按 ESC 或取消则不创建任何文件夹。
        """
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox

        dialog = QDialog(self)
        dialog.setWindowTitle(I18n.tr('new_folder'))
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout(dialog)

        label = QLabel(f"{I18n.tr('new_name')}:")
        layout.addWidget(label)

        # 预填默认名"新建文件夹"，全选便于直接输入替换
        default_name = I18n.tr('new_folder')
        name_edit = QLineEdit(default_name)
        layout.addWidget(name_edit)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        ok_btn = button_box.button(QDialogButtonBox.Ok)
        if ok_btn:
            ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
            ok_btn.setText(I18n.tr('ok'))
        cancel_btn = button_box.button(QDialogButtonBox.Cancel)
        if cancel_btn:
            cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
            cancel_btn.setText(I18n.tr('cancel'))

        layout.addWidget(button_box)

        name_edit.selectAll()
        name_edit.setFocus()

        # ESC / 取消 → dialog.exec() 返回 Rejected，直接返回不创建
        if dialog.exec() != QDialog.Accepted:
            return

        new_name = name_edit.text().strip()
        if not new_name:
            return

        # 校验文件名合法性
        if not self.is_valid_filename(new_name):
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("错误")
            msg_box.setText(I18n.tr('error_invalid_name'))
            msg_box.setIcon(QMessageBox.Warning)
            ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
            ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
            msg_box.exec()
            return

        new_path = self.current_path / new_name

        # 检查是否已存在
        if new_path.exists():
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("错误")
            msg_box.setText(I18n.tr('error_file_exists'))
            msg_box.setIcon(QMessageBox.Warning)
            ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
            ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
            msg_box.exec()
            return

        try:
            # 正式创建文件夹
            new_path.mkdir(parents=True, exist_ok=False)

            # 触发同步信号（本地操作）
            self.dir_created.emit(str(new_path))

            # 刷新文件列表
            self.load_files()
        except Exception as e:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("错误")
            msg_box.setText(f"创建文件夹失败: {str(e)}")
            msg_box.setIcon(QMessageBox.Critical)
            ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
            ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
            msg_box.exec()

    def rename_file(self):
        """重命名文件"""
        files = self.get_selected_files()
        if len(files) != 1:
            return
        
        old_path = files[0]
        old_name = old_path.name
        
        # 创建自定义对话框
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel, QDialogButtonBox
        dialog = QDialog(self)
        dialog.setWindowTitle(I18n.tr('rename'))
        dialog.setMinimumWidth(400)
        
        layout = QVBoxLayout(dialog)
        
        # 标签
        label = QLabel(f"{I18n.tr('new_name')}:")
        layout.addWidget(label)

        # 文件名输入框
        name_edit = QLineEdit(old_name)
        layout.addWidget(name_edit)

        # 按钮
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        # 应用全局按钮样式
        ok_btn = button_box.button(QDialogButtonBox.Ok)
        if ok_btn:
            ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
            ok_btn.setText(I18n.tr('ok'))
        cancel_btn = button_box.button(QDialogButtonBox.Cancel)
        if cancel_btn:
            cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
            cancel_btn.setText(I18n.tr('cancel'))
        
        layout.addWidget(button_box)
        
        # 选择文件名部分（不包括扩展名）
        if '.' in old_name and not old_name.startswith('.'):
            # 找到最后一个点的位置
            dot_pos = old_name.rfind('.')
            name_edit.setSelection(0, dot_pos)
        else:
            # 没有扩展名，选择全部
            name_edit.selectAll()
        
        # 显示对话框
        if dialog.exec() == QDialog.Accepted:
            new_name = name_edit.text().strip()
            
            if new_name and new_name != old_name:
                new_path = old_path.parent / new_name
                
                # 检查文件名是否合法
                if not self.is_valid_filename(new_name):
                    # 创建自定义警告框
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("错误")
                    msg_box.setText(I18n.tr('error_invalid_name'))
                    msg_box.setIcon(QMessageBox.Warning)
                    
                    # 添加自定义按钮
                    ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
                    ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
                    
                    msg_box.exec()
                    return
                
                # 检查文件是否已存在
                if new_path.exists():
                    # 创建自定义警告框
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("错误")
                    msg_box.setText(I18n.tr('error_file_exists'))
                    msg_box.setIcon(QMessageBox.Warning)
                    
                    # 添加自定义按钮
                    ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
                    ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
                    
                    msg_box.exec()
                    return
                
                try:
                    # 重命名前先取消该文件的传输任务，避免 Windows 文件锁导致重命名失败
                    import time
                    try:
                        old_rel_path = str(old_path.relative_to(self.folder_path)).replace('\\', '/')
                        if self._cancel_transfer_callback:
                            self._cancel_transfer_callback(old_rel_path)
                    except Exception:
                        pass
                    
                    # 循环尝试重命名，覆盖发送任务退出期间的最坏情况（最多 2 秒阻塞）
                    renamed = False
                    last_error = None
                    for attempt in range(30):
                        try:
                            old_path.rename(new_path)
                            renamed = True
                            break
                        except PermissionError as pe:
                            last_error = pe
                            time.sleep(0.1)
                        except OSError as oe:
                            last_error = oe
                            time.sleep(0.1)
                    
                    if not renamed:
                        raise last_error if last_error else Exception("重命名失败")
                    
                    # 只有本地操作才触发同步信号
                    if not self.is_syncing(str(old_path)):
                        self.file_renamed.emit(str(old_path), str(new_path))
                    self.load_files()
                except Exception as e:
                    # 创建自定义错误框
                    msg_box = QMessageBox(self)
                    msg_box.setWindowTitle("错误")
                    msg_box.setText(f"重命名失败: {str(e)}")
                    msg_box.setIcon(QMessageBox.Critical)
                    
                    # 添加自定义按钮
                    ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
                    ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
                    
                    msg_box.exec()
    
    def is_valid_filename(self, name: str) -> bool:
        """检查文件名是否合法"""
        if not name or len(name) > Config.MAX_FILE_NAME_LENGTH:
            return False
        
        for char in Config.FORBIDDEN_CHARS:
            if char in name:
                return False
        
        return True
    
    # ========== 拖拽功能 ==========
    
    def _handle_files_dragged(self, files: List[str], target_path: str, is_internal: bool):
        """处理拖拽的文件"""
        if is_internal:
            # 内部拖拽：移动或复制文件
            # 检查是否按住Ctrl键
            modifiers = QApplication.keyboardModifiers()
            if modifiers & Qt.ControlModifier:
                action = Qt.CopyAction
            else:
                action = Qt.MoveAction
            
            self._handle_internal_drop(files, action, target_path)
        else:
            # 外部拖拽：添加文件
            # 如果有目标文件夹，添加到目标文件夹
            if target_path:
                self.add_files(files, Path(target_path))
            else:
                self.add_files(files)
    
    def _handle_internal_drop(self, files: List[str], action, target_path: str = ""):
        """处理内部拖拽"""
        # 检查是否按住Ctrl键（复制）
        is_copy = (action == Qt.CopyAction)
        
        # 确定目标目录
        if target_path:
            target_dir = Path(target_path)
        else:
            target_dir = self.current_path
        
        for src_path in files:
            src = Path(src_path)
            if not src.exists():
                continue
            
            # 检查是否在同一目录
            if src.parent == target_dir:
                continue  # 同一目录，不操作
            
            # 检查是否拖拽到自己的子文件夹
            if src.is_dir() and target_dir.is_relative_to(src):
                # 创建自定义警告框
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("错误")
                msg_box.setText("不能将文件夹移动到自己的子文件夹中")
                msg_box.setIcon(QMessageBox.Warning)
                
                # 添加自定义按钮
                ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
                ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
                
                msg_box.exec()
                continue
            
            dst = target_dir / src.name
            
            # 检查目标文件是否存在
            if dst.exists():
                # 创建自定义消息框
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle(I18n.tr('confirm_replace'))
                msg_box.setText(f"文件 {src.name} 已存在，是否替换？")
                msg_box.setIcon(QMessageBox.Question)
                
                # 添加自定义按钮
                yes_btn = msg_box.addButton(I18n.tr('yes'), QMessageBox.YesRole)
                no_btn = msg_box.addButton(I18n.tr('no'), QMessageBox.NoRole)
                
                # 应用全局按钮样式
                yes_btn.setStyleSheet(BUTTON_STYLES['primary'])
                no_btn.setStyleSheet(BUTTON_STYLES['secondary'])
                
                msg_box.setDefaultButton(no_btn)
                msg_box.exec()
                
                if msg_box.clickedButton() == no_btn:
                    continue
            
            try:
                if is_copy:
                    # 复制文件
                    if src.is_dir():
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                    
                    # 触发同步信号
                    if not self.is_syncing(str(dst)):
                        self.file_added.emit(str(dst))
                else:
                    # 移动文件
                    src.rename(dst)
                    
                    # 触发同步信号
                    if not self.is_syncing(str(src)):
                        self.file_renamed.emit(str(src), str(dst))
                
            except Exception as e:
                self._show_error(f"操作失败: {str(e)}")
        
        # 刷新文件列表
        self.load_files()
    
    def add_files(self, file_paths: List[str], target_dir: Path = None):
        """添加文件到指定目录"""
        from ui.progress_dialog import CopyProgressDialog
        
        # 如果没有指定目标目录，使用当前目录
        if target_dir is None:
            target_dir = self.current_path
        
        # 检查是否有文件已存在
        existing_files = []
        for src_path in file_paths:
            src = Path(src_path)
            if not src.exists():
                continue
            
            dst = target_dir / src.name
            if dst.exists():
                existing_files.append(src.name)
        
        # 如果有文件已存在，显示确认对话框
        if existing_files:
            file_list = "\n".join(existing_files[:5])  # 最多显示5个文件
            if len(existing_files) > 5:
                file_list += f"\n... 还有 {len(existing_files) - 5} 个文件"
            
            # 创建自定义消息框
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle(I18n.tr('confirm_replace'))
            msg_box.setText(f"以下文件已存在，是否替换？\n\n{file_list}")
            msg_box.setIcon(QMessageBox.Question)
            
            # 添加自定义按钮
            yes_btn = msg_box.addButton(I18n.tr('yes'), QMessageBox.YesRole)
            no_btn = msg_box.addButton(I18n.tr('no'), QMessageBox.NoRole)
            cancel_btn = msg_box.addButton(I18n.tr('cancel'), QMessageBox.RejectRole)
            
            # 应用全局按钮样式
            yes_btn.setStyleSheet(BUTTON_STYLES['primary'])
            no_btn.setStyleSheet(BUTTON_STYLES['secondary'])
            cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
            
            msg_box.setDefaultButton(no_btn)
            msg_box.exec()
            
            clicked_btn = msg_box.clickedButton()
            
            if clicked_btn == cancel_btn:
                return
            elif clicked_btn == no_btn:
                # 移除已存在的文件
                file_paths = [
                    p for p in file_paths 
                    if Path(p).name not in existing_files
                ]
                if not file_paths:
                    return
        
        # 创建进度对话框
        progress_dialog = CopyProgressDialog(self)
        progress_dialog.setWindowTitle("复制文件")
        progress_dialog.show()
        
        # 创建工作线程
        self._copy_worker = FileCopyWorker(file_paths, target_dir)
        
        # 连接信号
        self._copy_worker.file_started.connect(
            lambda name: progress_dialog.set_filename(name)
        )
        self._copy_worker.progress_updated.connect(
            lambda idx, progress, total: progress_dialog.set_progress(progress)
        )
        self._copy_worker.file_finished.connect(
            lambda path: self._on_file_copied(path)
        )
        self._copy_worker.all_finished.connect(
            lambda: self._on_copy_finished(progress_dialog)
        )
        self._copy_worker.error_occurred.connect(
            lambda msg: self._show_error(msg)
        )
        progress_dialog.cancelled.connect(
            lambda: self._copy_worker.cancel()
        )
        
        # 启动工作线程
        self._copy_worker.start()
    
    def _on_file_copied(self, file_path: str):
        """文件复制完成回调"""
        # 立即发射信号，不等待对话框关闭
        # 只有本地操作才触发同步信号
        if not self.is_syncing(file_path):
            # 检查是否是文件夹
            path = Path(file_path)
            if path.is_dir():
                # 如果是文件夹，先发射文件夹创建信号
                self.file_added.emit(file_path)
                # 然后递归发射所有文件的信号
                self._emit_folder_files(path)
            else:
                # 如果是文件，直接发射信号
                self.file_added.emit(file_path)
    
    def _emit_folder_files(self, folder_path: Path):
        """递归发射文件夹内所有文件的信号"""
        try:
            for item in folder_path.rglob('*'):
                if item.is_file():
                    # 发射文件添加信号
                    self.file_added.emit(str(item))
        except Exception as e:
            print(f"发射文件夹文件信号失败: {e}")
    
    def _on_copy_finished(self, dialog):
        """所有文件复制完成回调"""
        # 立即关闭对话框，网络同步会在后台进行
        dialog.allow_close()
        dialog.close()
        # 刷新文件列表
        self.load_files()
    
    def _show_error(self, message: str):
        """显示错误消息（在主线程中调用）"""
        # 创建自定义错误框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("错误")
        msg_box.setText(message)
        msg_box.setIcon(QMessageBox.Critical)
        
        # 添加自定义按钮
        ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
        ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
        
        msg_box.exec()
    
    def refresh(self):
        """
        刷新文件列表（用于接收远程文件后）
        不会触发同步信号
        """
        self.load_files()