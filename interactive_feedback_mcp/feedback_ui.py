# Interactive Feedback MCP UI
# Developed by Fábio Ferreira (https://x.com/fabiomlferreira)
# Inspired by/related to dotcursorrules.com (https://dotcursorrules.com/)
import os
import sys
import json
import psutil
import argparse
import subprocess
import threading
import hashlib
import uuid
import datetime
from typing import Optional, TypedDict, List

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QTextEdit, QGroupBox, QDialog, QListWidget, QDialogButtonBox, QComboBox, QFileDialog,
    QScrollArea, QFrame, QGridLayout, QMessageBox, QTabWidget, QSizePolicy
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer, QSettings, QPoint, QSize, QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QTextCursor, QIcon, QKeyEvent, QFont, QFontDatabase, QPalette, QColor, QPixmap, QImage, QClipboard, QShortcut, QKeySequence

class FeedbackResult(TypedDict):
    command_logs: str
    interactive_feedback: str
    uploaded_images: list[str]

class FeedbackConfig(TypedDict):
    run_command: str
    execute_automatically: bool

def generate_random_filename(extension: str = "jpg") -> str:
    """生成随机文件名，格式为：年月日_时分秒_uuid.扩展名"""
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    random_str = str(uuid.uuid4())[:8]  # 取UUID的前8位
    return f"{timestamp}_{random_str}.{extension}"

def ensure_temp_directory(base_dir: str) -> str:
    """确保temp目录存在，返回temp目录的完整路径"""
    temp_dir = os.path.join(base_dir, "temp")
    if not os.path.exists(temp_dir):
        os.makedirs(temp_dir)
    return temp_dir

def kill_tree(process: subprocess.Popen):
    killed: list[psutil.Process] = []
    parent = psutil.Process(process.pid)
    for proc in parent.children(recursive=True):
        try:
            proc.kill()
            killed.append(proc)
        except psutil.Error:
            pass
    try:
        parent.kill()
    except psutil.Error:
        pass
    killed.append(parent)

    # Terminate any remaining processes
    for proc in killed:
        try:
            if proc.is_running():
                proc.terminate()
        except psutil.Error:
            pass

def get_user_environment() -> dict[str, str]:
    if sys.platform != "win32":
        return os.environ.copy()

    import ctypes
    from ctypes import wintypes

    # Load required DLLs
    advapi32 = ctypes.WinDLL("advapi32")
    userenv = ctypes.WinDLL("userenv")
    kernel32 = ctypes.WinDLL("kernel32")

    # Constants
    TOKEN_QUERY = 0x0008

    # Function prototypes
    OpenProcessToken = advapi32.OpenProcessToken
    OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    OpenProcessToken.restype = wintypes.BOOL

    CreateEnvironmentBlock = userenv.CreateEnvironmentBlock
    CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    CreateEnvironmentBlock.restype = wintypes.BOOL

    DestroyEnvironmentBlock = userenv.DestroyEnvironmentBlock
    DestroyEnvironmentBlock.argtypes = [wintypes.LPVOID]
    DestroyEnvironmentBlock.restype = wintypes.BOOL

    GetCurrentProcess = kernel32.GetCurrentProcess
    GetCurrentProcess.argtypes = []
    GetCurrentProcess.restype = wintypes.HANDLE

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    # Get process token
    token = wintypes.HANDLE()
    if not OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise RuntimeError("Failed to open process token")

    try:
        # Create environment block
        environment = ctypes.c_void_p()
        if not CreateEnvironmentBlock(ctypes.byref(environment), token, False):
            raise RuntimeError("Failed to create environment block")

        try:
            # Convert environment block to list of strings
            result = {}
            env_ptr = ctypes.cast(environment, ctypes.POINTER(ctypes.c_wchar))
            offset = 0

            while True:
                # Get string at current offset
                current_string = ""
                while env_ptr[offset] != "\0":
                    current_string += env_ptr[offset]
                    offset += 1

                # Skip null terminator
                offset += 1

                # Break if we hit double null terminator
                if not current_string:
                    break

                equal_index = current_string.index("=")
                if equal_index == -1:
                    continue

                key = current_string[:equal_index]
                value = current_string[equal_index + 1:]
                result[key] = value

            return result

        finally:
            DestroyEnvironmentBlock(environment)

    finally:
        CloseHandle(token)

class FeedbackTextEdit(QTextEdit):
    def __init__(self, parent=None, feedback_ui=None):
        super().__init__(parent)
        self.setAcceptRichText(False)  # 禁用富文本粘贴，只允许纯文本
        self.feedback_ui = feedback_ui  # 保存对FeedbackUI实例的引用

    def keyPressEvent(self, event: QKeyEvent):
        # 检查是否按下Ctrl+Enter或Ctrl+Return
        is_enter = (event.key() == Qt.Key_Return or event.key() == Qt.Key_Enter)
        is_ctrl = (event.modifiers() & Qt.ControlModifier) == Qt.ControlModifier

        if is_enter and is_ctrl:
            # 尝试查找父FeedbackUI实例
            if self.feedback_ui:
                # 直接使用已保存的引用
                self.feedback_ui._submit_feedback()
            else:
                # 备用方法：通过parent()查找
                parent = self.parent()
                while parent and not isinstance(parent, FeedbackUI):
                    parent = parent.parent()
                if parent:
                    parent._submit_feedback()
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event):
        """当文本框获得焦点时停止自动提交倒计时"""
        super().focusInEvent(event)
        if self.feedback_ui:
            self.feedback_ui._stop_auto_submit_countdown()

    def mousePressEvent(self, event):
        """当鼠标点击文本框时停止自动提交倒计时"""
        super().mousePressEvent(event)
        if self.feedback_ui:
            self.feedback_ui._stop_auto_submit_countdown()
    
    def insertFromMimeData(self, source):
        """重写粘贴方法，支持粘贴图片"""
        if source.hasImage() and self.feedback_ui:
            # 剪贴板中有图片且有FeedbackUI引用时
            result = self.feedback_ui._get_clipboard_image(show_message=False)
            if not result:
                # 图片处理失败时，尝试粘贴为文本
                super().insertFromMimeData(source)
        else:
            # 没有图片或没有FeedbackUI引用时，按原方式处理
            super().insertFromMimeData(source)

class QuickReplyEditDialog(QDialog):
    """用于编辑快捷回复的对话框"""
    def __init__(self, parent=None, quick_replies=None):
        super().__init__(parent)
        self.setWindowTitle("编辑快捷回复")
        self.setMinimumWidth(400)
        self.setMinimumHeight(300)
        
        # 初始化快捷回复列表
        self.quick_replies = quick_replies or []
        
        # 创建UI
        layout = QVBoxLayout(self)
        
        # 添加说明标签
        label = QLabel("编辑、添加或删除快捷回复项目:")
        layout.addWidget(label)
        
        # 创建列表显示当前快捷回复
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SingleSelection)
        # 添加现有快捷回复到列表
        for reply in self.quick_replies:
            self.list_widget.addItem(reply)
        layout.addWidget(self.list_widget)
        
        # 编辑区域
        edit_layout = QHBoxLayout()
        self.edit_input = QLineEdit()
        self.edit_input.setPlaceholderText("输入新的快捷回复...")
        edit_layout.addWidget(self.edit_input)
        
        # 添加按钮
        self.add_button = QPushButton("添加")
        self.add_button.clicked.connect(self._add_reply)
        edit_layout.addWidget(self.add_button)
        
        layout.addLayout(edit_layout)
        
        # 操作按钮行
        button_layout = QHBoxLayout()
        
        # 删除按钮
        self.delete_button = QPushButton("删除所选")
        self.delete_button.clicked.connect(self._delete_reply)
        button_layout.addWidget(self.delete_button)
        
        # 上移按钮
        self.move_up_button = QPushButton("上移")
        self.move_up_button.clicked.connect(self._move_up)
        button_layout.addWidget(self.move_up_button)
        
        # 下移按钮
        self.move_down_button = QPushButton("下移")
        self.move_down_button.clicked.connect(self._move_down)
        button_layout.addWidget(self.move_down_button)
        
        layout.addLayout(button_layout)
        
        # 底部按钮行（确定/取消）
        dialog_buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        dialog_buttons.accepted.connect(self.accept)
        dialog_buttons.rejected.connect(self.reject)
        layout.addWidget(dialog_buttons)
        
        # 连接列表项被选中的信号
        self.list_widget.itemSelectionChanged.connect(self._selection_changed)
        self.list_widget.itemDoubleClicked.connect(self._edit_item)
        
        # 初始化按钮状态
        self._selection_changed()
    
    def _add_reply(self):
        """添加新的快捷回复"""
        text = self.edit_input.text().strip()
        if text:
            self.list_widget.addItem(text)
            self.edit_input.clear()
    
    def _delete_reply(self):
        """删除选中的快捷回复"""
        selected_items = self.list_widget.selectedItems()
        if selected_items:
            for item in selected_items:
                row = self.list_widget.row(item)
                self.list_widget.takeItem(row)
    
    def _move_up(self):
        """上移选中的项目"""
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            return
            
        current_row = self.list_widget.row(selected_items[0])
        if current_row > 0:
            item = self.list_widget.takeItem(current_row)
            self.list_widget.insertItem(current_row - 1, item)
            self.list_widget.setCurrentItem(item)
    
    def _move_down(self):
        """下移选中的项目"""
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            return
            
        current_row = self.list_widget.row(selected_items[0])
        if current_row < self.list_widget.count() - 1:
            item = self.list_widget.takeItem(current_row)
            self.list_widget.insertItem(current_row + 1, item)
            self.list_widget.setCurrentItem(item)
    
    def _selection_changed(self):
        """当列表选择变化时更新按钮状态"""
        has_selection = bool(self.list_widget.selectedItems())
        self.delete_button.setEnabled(has_selection)
        self.move_up_button.setEnabled(has_selection and self.list_widget.currentRow() > 0)
        self.move_down_button.setEnabled(has_selection and self.list_widget.currentRow() < self.list_widget.count() - 1)
    
    def _edit_item(self, item):
        """双击编辑项目"""
        self.edit_input.setText(item.text())
        self.list_widget.takeItem(self.list_widget.row(item))
    
    def get_quick_replies(self):
        """获取编辑后的快捷回复列表"""
        result = []
        for i in range(self.list_widget.count()):
            result.append(self.list_widget.item(i).text())
        return result

class LogSignals(QObject):
    append_log = Signal(str)

class FeedbackUI(QMainWindow):
    def __init__(self, project_directory: str, prompt: str):
        super().__init__()
        self.project_directory = project_directory
        self.prompt = prompt

        # 设置应用程序使用Fusion样式，这是一个跨平台的样式，最接近原生外观
        QApplication.setStyle("Fusion")
        
        self.process: Optional[subprocess.Popen] = None
        self.log_buffer = []
        self.feedback_result = None
        self.log_signals = LogSignals()
        self.log_signals.append_log.connect(self._append_log)
        
        # 初始化上传图片路径列表
        self.uploaded_images = []
        
        # 窗口状态记录（用于记录显示/隐藏终端时的窗口状态）
        self.window_state_with_terminal = None  # 存储终端显示时的窗口大小和位置
        self.window_state_without_terminal = None  # 存储终端隐藏时的窗口大小和位置
        
        # 确保temp目录存在
        self.temp_dir = ensure_temp_directory(self.project_directory)
        
        # 图片预览相关
        self.image_previews = []  # 存储图片预览控件的列表
        self.image_labels = []    # 存储图片标签控件的列表

        # 自动提交相关
        self.auto_submit_enabled = False  # 是否启用自动提交
        self.auto_submit_wait_time = 60   # 等待时间（秒）
        self.auto_submit_timer = None     # 倒计时定时器
        self.countdown_remaining = 0      # 剩余倒计时时间
        self.original_submit_text = ""    # 原始提交按钮文本

        # 窗口大小设置
        self.default_size = (460, 360)
        self.size_multiplier = 1
        self.size_states = [1, 2, 3]  # 窗口大小倍数状态

        self.setWindowTitle("AI超级助手 MCP qq:1715109585")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        icon_path = os.path.join(script_dir, "images", "feedback.png")
        self.setWindowIcon(QIcon(icon_path))

        # 设置默认窗口大小
        self.resize(*self.default_size)

        self.settings = QSettings("InteractiveFeedbackMCP", "InteractiveFeedbackMCP")

        # 根据设置决定是否启用窗口置顶
        self.settings.beginGroup("MainWindow_General")
        stay_on_top_enabled = self.settings.value("stay_on_top_enabled", True, type=bool)
        self.settings.endGroup()

        if stay_on_top_enabled:
            self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(self.windowFlags() & ~Qt.WindowStaysOnTopHint)
        
        # Load general UI settings for the main window (geometry, state)
        self.settings.beginGroup("MainWindow_General")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        else:
            # 设置窗口位置在屏幕右下角
            self._position_window_bottom_right()
        state = self.settings.value("windowState")
        if state:
            self.restoreState(state)
            
        # 加载窗口大小设置
        custom_width = self.settings.value("custom_width", -1, type=int)
        custom_height = self.settings.value("custom_height", -1, type=int)
        if custom_width > 0 and custom_height > 0:
            self.resize(custom_width, custom_height)
        else:
            # 仅在没有自定义大小设置时使用默认大小
            self.resize(*self.default_size)
            
        # 检查是否有用户保存的自定义位置
        self.use_custom_position = self.settings.value("use_custom_position", False, type=bool)
        custom_x = self.settings.value("custom_position_x", -1, type=int)
        custom_y = self.settings.value("custom_position_y", -1, type=int)
        self.custom_position = None
        if custom_x >= 0 and custom_y >= 0:
            from PySide6.QtCore import QPoint
            self.custom_position = QPoint(custom_x, custom_y)
            
        # 加载快捷回复设置
        self.quick_replies = self.settings.value("quick_replies", [], type=list)
        # 如果没有保存的快捷回复，使用默认值
        if not self.quick_replies:
            self.quick_replies = ["继续", "结束对话","使用MODE: RESEARCH重新开始"]
        self.settings.endGroup() # End "MainWindow_General" group
        
        # Load project-specific settings (command, auto-execute, selected tab index)
        self.project_group_name = get_project_settings_group(self.project_directory)
        self.settings.beginGroup(self.project_group_name)
        loaded_run_command = self.settings.value("run_command", "", type=str)
        loaded_execute_auto = self.settings.value("execute_automatically", False, type=bool)
        self.settings.endGroup() # End project-specific group
        
        self.config: FeedbackConfig = {
            "run_command": loaded_run_command,
            "execute_automatically": loaded_execute_auto
        }

        self._create_ui() # self.config is used here to set initial values
        
        # 确保窗口位于正确位置
        QTimer.singleShot(0, self._position_window_bottom_right)
        
        # 初始化图片预览
        QTimer.singleShot(100, self._update_image_preview)
        
        # 连接窗口大小变化信号，确保窗口调整后更新大小信息标签
        self.resize_event_timer = QTimer()
        self.resize_event_timer.setSingleShot(True)
        self.resize_event_timer.timeout.connect(self._update_size_info)

        if self.config.get("execute_automatically", False):
            self._run_command()

    def _position_window_bottom_right(self):
        """将窗口定位在屏幕右下角或用户自定义位置"""
        # 如果有用户保存的自定义位置，优先使用
        if hasattr(self, 'use_custom_position') and self.use_custom_position and self.custom_position:
            self.move(self.custom_position)
            return
            
        # 否则使用默认的右下角位置
        current_width = self.width()
        current_height = self.height()
        screen_geometry = QApplication.primaryScreen().availableGeometry()
        x = screen_geometry.width() - current_width - 20  # 右边距20像素
        y = screen_geometry.height() - current_height - 40  # 下边距40像素
        self.move(x, y)

    def _format_windows_path(self, path: str) -> str:
        if sys.platform == "win32":
            # Convert forward slashes to backslashes
            path = path.replace("/", "\\")
            # Capitalize drive letter if path starts with x:\
            if len(path) >= 2 and path[1] == ":" and path[0].isalpha():
                path = path[0].upper() + path[1:]
        return path

    def _create_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # 创建选项卡控件
        self.tab_widget = QTabWidget()
        self.tab_widget.setDocumentMode(False)
        
        # 准备"反馈"、"终端"和"设置"选项卡的内容
        
        # Command section - 将作为"终端"选项卡的内容
        self.command_group = QGroupBox()  # 不再需要标题，因为选项卡有标签
        command_layout = QVBoxLayout(self.command_group)

        # Working directory label
        formatted_path = self._format_windows_path(self.project_directory)
        working_dir_label = QLabel(f"工作目录: {formatted_path}")
        command_layout.addWidget(working_dir_label)

        # Command input row
        command_input_layout = QHBoxLayout()
        self.command_entry = QLineEdit()
        self.command_entry.setText(self.config["run_command"])
        self.command_entry.returnPressed.connect(self._run_command)
        self.command_entry.textChanged.connect(self._update_config)
        self.run_button = QPushButton("运行(&R)")
        self.run_button.clicked.connect(self._run_command)
        self.run_button.setMinimumWidth(80)
        self.run_button.setMinimumHeight(30)
        self.run_button.setAutoFillBackground(True)  # 设置自动填充背景

        command_input_layout.addWidget(self.command_entry)
        command_input_layout.addWidget(self.run_button)
        command_layout.addLayout(command_input_layout)

        # Auto-execute and save config row
        auto_layout = QHBoxLayout()
        self.auto_check = QCheckBox("下次自动执行（打开此应用时自动运行命令）")
        self.auto_check.setChecked(self.config.get("execute_automatically", False))
        self.auto_check.stateChanged.connect(self._update_config)

        save_button = QPushButton("保存命令(&S)")
        save_button.clicked.connect(self._save_config)
        save_button.setMinimumWidth(100)
        save_button.setMinimumHeight(30)
        save_button.setAutoFillBackground(True)  # 设置自动填充背景

        auto_layout.addWidget(self.auto_check)
        auto_layout.addStretch()
        auto_layout.addWidget(save_button)
        command_layout.addLayout(auto_layout)

        # Console section (now part of command_group)
        console_group = QGroupBox("控制台")
        console_layout_internal = QVBoxLayout(console_group)
        console_group.setMinimumHeight(200)

        # Log text area
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(False)  # 设置为可编辑
        font = QFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        font.setPointSize(9)
        self.log_text.setFont(font)
        console_layout_internal.addWidget(self.log_text)

        # Control buttons
        button_layout = QHBoxLayout()
        self.clear_button = QPushButton("清除(&C)")
        self.clear_button.clicked.connect(self.clear_logs)
        self.clear_button.setMinimumWidth(80)
        self.clear_button.setMinimumHeight(30)
        self.clear_button.setAutoFillBackground(True)  # 设置自动填充背景
        
        button_layout.addStretch()
        button_layout.addWidget(self.clear_button)
        console_layout_internal.addLayout(button_layout)
        
        command_layout.addWidget(console_group)

        # Feedback section - 将作为"反馈"选项卡的内容
        self.feedback_group = QGroupBox()  # 不再需要标题，因为选项卡有标签
        feedback_layout = QVBoxLayout(self.feedback_group)

        # Short description label (from self.prompt)
        self.description_label = QLabel(self.prompt)
        self.description_label.setWordWrap(True)
        feedback_layout.addWidget(self.description_label)

        self.feedback_text = FeedbackTextEdit(feedback_ui=self)
        font_metrics = self.feedback_text.fontMetrics()
        row_height = font_metrics.height()
        # Calculate height for 5 lines + some padding for margins
        padding = self.feedback_text.contentsMargins().top() + self.feedback_text.contentsMargins().bottom() + 5 # 5 is extra vertical padding
        self.feedback_text.setMinimumHeight(5 * row_height + padding)

        self.feedback_text.setPlaceholderText("在此输入您的反馈 (按Ctrl+Enter提交)")
        
        # 根据系统类型设置快捷键提示
        if sys.platform == "darwin":  # macOS
            submit_button_text = "发送反馈(&S) (Cmd+Enter)"
            shortcut_text = "Cmd+Enter"
        else:  # Windows, Linux等其他系统
            submit_button_text = "发送反馈(&S) (Ctrl+Enter)"
            shortcut_text = "Ctrl+Enter"
        
        # 更新占位符文本
        self.feedback_text.setPlaceholderText(f"在此输入您的反馈 (按{shortcut_text}提交)")
        
        # 为文本框添加全局快捷键支持
        submit_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        submit_shortcut.activated.connect(self._submit_feedback)
        
        # 在Windows上额外添加Ctrl+Enter快捷键
        if sys.platform == "win32":
            enter_shortcut = QShortcut(QKeySequence("Ctrl+Enter"), self)
            enter_shortcut.activated.connect(self._submit_feedback)
        
        submit_button = QPushButton(submit_button_text)
        submit_button.setAutoFillBackground(True)
        submit_button.clicked.connect(self._submit_feedback)
        submit_button.setMinimumWidth(200)
        submit_button.setMinimumHeight(60)
        # 设置大小策略为自适应
        submit_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # 保存按钮引用和原始文本，用于自动提交倒计时
        self.submit_button = submit_button
        self.original_submit_text = submit_button_text
        
        # 创建水平布局来包含提交按钮
        submit_layout = QHBoxLayout()
        submit_layout.addWidget(submit_button)
        
        feedback_layout.addWidget(self.feedback_text)
        
        # 第一行：开始实施和快捷回复相关按钮
        quick_reply_layout = QHBoxLayout()
        
        # 开始实施按钮 - 放在最左侧
        start_button = QPushButton("开始实施")
        start_button.clicked.connect(lambda: self._insert_quick_reply("开始实施"))
        start_button.setMinimumHeight(30)
        start_button.setMinimumWidth(100)  # 设置最小宽度
        start_button.setAutoFillBackground(True)
        # 设置大小策略为Expanding，允许按钮水平方向扩展
        start_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        quick_reply_layout.addWidget(start_button, 1)  # 设置拉伸因子为1
        
        # 快捷回复组合框 - 放在中间
        self.quick_reply_combo = QComboBox()
        self.quick_reply_combo.setMinimumHeight(30)
        self.quick_reply_combo.setMinimumWidth(180)
        # 设置大小策略为Expanding，允许组合框水平方向扩展
        self.quick_reply_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        # 添加快捷回复选项
        for reply in self.quick_replies:
            self.quick_reply_combo.addItem(reply)
        # 连接信号：当选择变更时自动填入文本框
        self.quick_reply_combo.activated.connect(self._apply_selected_quick_reply)
        quick_reply_layout.addWidget(self.quick_reply_combo, 2)  # 设置拉伸因子为2，使其比其他按钮占更多空间
        
        # 编辑快捷回复按钮 - 放在最右侧
        edit_replies_button = QPushButton("编辑快捷回复")
        edit_replies_button.clicked.connect(self._edit_quick_replies)
        edit_replies_button.setMinimumHeight(30)
        edit_replies_button.setMinimumWidth(120)  # 设置最小宽度
        edit_replies_button.setAutoFillBackground(True)
        # 设置大小策略为Expanding，允许按钮水平方向扩展
        edit_replies_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        quick_reply_layout.addWidget(edit_replies_button, 1)  # 设置拉伸因子为1
        
        # 设置布局的间距，让按钮之间有适当的空间
        quick_reply_layout.setSpacing(10)
        
        feedback_layout.addLayout(quick_reply_layout)
        
        # 第二行：图片相关按钮
        image_buttons_layout = QHBoxLayout()
        
        # 上传图片按钮
        upload_image_button = QPushButton("上传图片")
        upload_image_button.clicked.connect(self._upload_image)
        upload_image_button.setMinimumHeight(30)
        upload_image_button.setMinimumWidth(100)  # 设置最小宽度
        upload_image_button.setAutoFillBackground(True)
        # 设置大小策略为Expanding，允许按钮水平方向扩展
        upload_image_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        image_buttons_layout.addWidget(upload_image_button, 1)  # 设置拉伸因子为1
        
        # 从剪贴板获取图片按钮
        clipboard_image_button = QPushButton("从剪贴板获取图片")
        clipboard_image_button.clicked.connect(self._get_clipboard_image)
        clipboard_image_button.setMinimumHeight(30)
        clipboard_image_button.setMinimumWidth(150)  # 设置最小宽度
        clipboard_image_button.setAutoFillBackground(True)
        # 设置大小策略为Expanding，允许按钮水平方向扩展
        clipboard_image_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        image_buttons_layout.addWidget(clipboard_image_button, 1)  # 设置拉伸因子为1
        
        # 添加弹性空间，使按钮能够在窗口调整大小时适当拉伸
        image_buttons_layout.addStretch(0.5)
        
        # 设置布局的间距，让按钮之间有适当的空间
        image_buttons_layout.setSpacing(10)
        
        feedback_layout.addLayout(image_buttons_layout)
        
        # 添加图片预览区域
        preview_group = QGroupBox("图片预览")
        self.preview_group = preview_group  # 保存为类成员变量
        preview_layout = QVBoxLayout(preview_group)
        
        # 创建滚动区域用于图片预览
        self.preview_scroll = QScrollArea()
        self.preview_scroll.setWidgetResizable(True)
        self.preview_scroll.setMinimumHeight(100)
        self.preview_scroll.setMaximumHeight(200)
        
        # 创建图片预览的容器
        self.preview_container = QWidget()
        self.preview_grid = QGridLayout(self.preview_container)
        self.preview_grid.setSpacing(10)
        self.preview_scroll.setWidget(self.preview_container)
        
        preview_layout.addWidget(self.preview_scroll)
        feedback_layout.addWidget(preview_group)
        
        feedback_layout.addLayout(submit_layout)

        # 设置feedback_group的最小高度
        self.feedback_group.setMinimumHeight(self.description_label.sizeHint().height() + 
                                            self.feedback_text.minimumHeight() + 
                                            start_button.sizeHint().height() + 
                                            edit_replies_button.sizeHint().height() + 
                                            upload_image_button.sizeHint().height() + 
                                            clipboard_image_button.sizeHint().height() + 
                                            submit_button.sizeHint().height() + 30) # 增加额外间距

        # 将内容添加到选项卡
        self.tab_widget.addTab(self.feedback_group, "反馈")
        self.tab_widget.addTab(self.command_group, "终端")
        
        # 创建设置选项卡
        self.settings_group = QGroupBox()
        settings_layout = QVBoxLayout(self.settings_group)
        
        # 窗口大小设置部分
        size_group = QGroupBox("窗口大小")
        size_layout = QVBoxLayout(size_group)
        
        # 窗口大小调整按钮
        self.resize_button = QPushButton("调整窗口大小")
        self.resize_button.clicked.connect(self._cycle_window_size)
        self.resize_button.setMinimumWidth(200)
        self.resize_button.setMinimumHeight(40)
        self.resize_button.setAutoFillBackground(True)
        self.resize_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        size_layout.addWidget(self.resize_button)
        
        # 添加当前窗口大小的提示标签
        self.size_info_label = QLabel(f"当前窗口大小: {self.width()} x {self.height()}")
        size_layout.addWidget(self.size_info_label)
        
        # 窗口位置设置部分
        position_group = QGroupBox("窗口位置")
        position_layout = QVBoxLayout(position_group)
        
        # 自动保存窗口位置的选项
        self.auto_save_position_check = QCheckBox("关闭窗口时自动保存位置")
        self.auto_save_position_check.setChecked(True)  # 默认启用
        position_layout.addWidget(self.auto_save_position_check)
        
        # 从设置中读取自动保存窗口位置的选项（如果有）
        self.settings.beginGroup("MainWindow_General")
        auto_save_position = self.settings.value("auto_save_position", True, type=bool)
        self.auto_save_position_check.setChecked(auto_save_position)

        # 读取自动提交设置
        self.auto_submit_enabled = self.settings.value("auto_submit_enabled", False, type=bool)
        self.auto_submit_wait_time = self.settings.value("auto_submit_wait_time", 60, type=int)

        self.settings.endGroup()
        
        # 连接状态变化信号
        self.auto_save_position_check.stateChanged.connect(self._update_auto_save_position)
        
        # 手动保存当前窗口位置的按钮
        save_position_button = QPushButton("立即保存当前窗口位置")
        save_position_button.clicked.connect(self._save_window_position)
        save_position_button.setMinimumWidth(200)
        save_position_button.setMinimumHeight(40)
        save_position_button.setAutoFillBackground(True)
        save_position_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        position_layout.addWidget(save_position_button)
        
        # 重置窗口位置的按钮
        reset_position_button = QPushButton("重置窗口位置到屏幕右下角")
        reset_position_button.clicked.connect(self._reset_window_position)
        reset_position_button.setMinimumWidth(200)
        reset_position_button.setMinimumHeight(40)
        reset_position_button.setAutoFillBackground(True)
        reset_position_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        position_layout.addWidget(reset_position_button)
        
        # 自动提交设置部分
        auto_submit_group = QGroupBox("自动提交设置")
        auto_submit_layout = QVBoxLayout(auto_submit_group)

        # 启用自动提交的勾选框
        self.auto_submit_check = QCheckBox("启用自动提交")
        self.auto_submit_check.setChecked(self.auto_submit_enabled)
        self.auto_submit_check.stateChanged.connect(self._update_auto_submit_settings)
        auto_submit_layout.addWidget(self.auto_submit_check)

        # 等待时间设置
        time_layout = QHBoxLayout()
        time_label = QLabel("等待时间（秒）:")
        self.auto_submit_time_input = QLineEdit()
        self.auto_submit_time_input.setText(str(self.auto_submit_wait_time))
        self.auto_submit_time_input.setMaximumWidth(100)
        self.auto_submit_time_input.textChanged.connect(self._update_auto_submit_settings)

        time_layout.addWidget(time_label)
        time_layout.addWidget(self.auto_submit_time_input)
        time_layout.addStretch()
        auto_submit_layout.addLayout(time_layout)

        # 说明文字
        info_label = QLabel("启用后，打开窗口时将自动开始倒计时，时间到后自动提交反馈。\n点击反馈文本框可停止自动提交。")
        info_label.setWordWrap(True)
        info_label.setStyleSheet("color: gray; font-size: 11px;")
        auto_submit_layout.addWidget(info_label)

        # 窗口置顶设置部分
        stay_on_top_group = QGroupBox("窗口置顶设置")
        stay_on_top_layout = QVBoxLayout(stay_on_top_group)

        # 启动时置顶的勾选框
        self.stay_on_top_check = QCheckBox("启动时窗口置顶")
        # 从设置中读取置顶选项
        self.settings.beginGroup("MainWindow_General")
        stay_on_top_enabled = self.settings.value("stay_on_top_enabled", True, type=bool)
        self.stay_on_top_check.setChecked(stay_on_top_enabled)
        self.settings.endGroup()

        self.stay_on_top_check.stateChanged.connect(self._update_stay_on_top_setting)
        stay_on_top_layout.addWidget(self.stay_on_top_check)

        # 立即切换置顶状态的按钮
        toggle_top_button = QPushButton("切换窗口置顶状态")
        toggle_top_button.clicked.connect(self._toggle_stay_on_top)
        toggle_top_button.setMinimumWidth(200)
        toggle_top_button.setMinimumHeight(40)
        toggle_top_button.setAutoFillBackground(True)
        toggle_top_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        stay_on_top_layout.addWidget(toggle_top_button)

        # 说明文字
        stay_on_top_info = QLabel("启用后，每次启动时窗口将自动置顶。\n也可以使用按钮立即切换当前窗口的置顶状态。")
        stay_on_top_info.setWordWrap(True)
        stay_on_top_info.setStyleSheet("color: gray; font-size: 11px;")
        stay_on_top_layout.addWidget(stay_on_top_info)

        # 将分组添加到设置布局
        settings_layout.addWidget(size_group)
        settings_layout.addWidget(position_group)
        settings_layout.addWidget(auto_submit_group)
        settings_layout.addWidget(stay_on_top_group)
        settings_layout.addStretch()
        
        # 创建工具选项卡
        self.tools_group = QGroupBox()
        tools_layout = QVBoxLayout(self.tools_group)

        # Git AI Commit GUI 工具部分
        git_commit_group = QGroupBox("Git 提交工具")
        git_commit_layout = QVBoxLayout(git_commit_group)

        # 说明文字
        git_commit_info = QLabel("使用 AI 辅助生成 Git 提交信息")
        git_commit_info.setWordWrap(True)
        git_commit_info.setStyleSheet("color: gray; font-size: 11px; margin-bottom: 10px;")
        git_commit_layout.addWidget(git_commit_info)

        # Git AI Commit GUI 按钮
        self.git_commit_button = QPushButton("运行 Git AI Commit GUI")
        self.git_commit_button.clicked.connect(self._run_git_ai_commit_gui)
        self.git_commit_button.setMinimumWidth(200)
        self.git_commit_button.setMinimumHeight(50)
        self.git_commit_button.setAutoFillBackground(True)
        self.git_commit_button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        git_commit_layout.addWidget(self.git_commit_button)

        # 添加到工具布局
        tools_layout.addWidget(git_commit_group)
        tools_layout.addStretch()

        # 添加设置选项卡
        self.tab_widget.addTab(self.settings_group, "设置")

        # 添加工具选项卡
        self.tab_widget.addTab(self.tools_group, "工具")

        # 默认选择反馈选项卡（索引0）
        self.tab_widget.setCurrentIndex(0)
        
        # 连接选项卡切换信号，保存当前选中的选项卡
        self.tab_widget.currentChanged.connect(self._tab_changed)
        
        # 将选项卡控件添加到主布局
        layout.addWidget(self.tab_widget)

    def _cycle_window_size(self):
        """循环调整窗口大小：默认 -> x2 -> x3 -> 默认"""
        # 获取当前倍数索引
        current_index = self.size_states.index(self.size_multiplier)
        # 计算下一个倍数索引（循环）
        next_index = (current_index + 1) % len(self.size_states)
        # 设置新的倍数
        self.size_multiplier = self.size_states[next_index]
        
        # 获取当前窗口实际大小
        current_width = self.width()
        current_height = self.height()
        
        # 使用当前窗口大小作为基准，而不是默认大小
        new_width = int(current_width * self.size_multiplier / self.size_states[current_index])
        new_height = int(current_height * self.size_multiplier / self.size_states[current_index])
        
        # 调整窗口大小
        self.resize(new_width, new_height)
        
        # 如果使用自定义位置，保持当前位置
        if hasattr(self, 'use_custom_position') and self.use_custom_position and self.custom_position:
            # 不移动窗口，保持当前位置
            pass
        else:
            # 重新计算窗口位置，保持在屏幕右下角
            screen_geometry = QApplication.primaryScreen().availableGeometry()
            x = screen_geometry.width() - new_width - 20  # 右边距20像素
            y = screen_geometry.height() - new_height - 40  # 下边距40像素
            self.move(x, y)
        
        # 更新按钮文本和大小信息标签
        if self.size_multiplier == 1:
            self.resize_button.setText("调整窗口大小")
        else:
            self.resize_button.setText(f"窗口大小 x{self.size_multiplier}")
            
        # 更新大小信息标签
        if hasattr(self, 'size_info_label'):
            self.size_info_label.setText(f"当前窗口大小: {new_width} x {new_height}")
            
    def _tab_changed(self, index):
        """处理选项卡切换事件"""
        # 保存当前选中的选项卡索引
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("selectedTabIndex", index)
        self.settings.endGroup()
        
    def _update_config(self):
        self.config["run_command"] = self.command_entry.text()
        self.config["execute_automatically"] = self.auto_check.isChecked()

    def _append_log(self, text: str):
        self.log_buffer.append(text)
        self.log_text.append(text.rstrip())
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_text.setTextCursor(cursor)

    def _check_process_status(self):
        if self.process and self.process.poll() is not None:
            # Process has terminated
            exit_code = self.process.poll()
            self._append_log(f"\n进程已退出，退出代码 {exit_code}\n")
            self.run_button.setText("运行(&R)")
            self.process = None
            self.activateWindow()
            self.feedback_text.setFocus()

    def _run_command(self):
        if self.process:
            kill_tree(self.process)
            self.process = None
            self.run_button.setText("运行(&R)")
            return

        # Clear the log buffer but keep UI logs visible
        self.log_buffer = []

        command = self.command_entry.text()
        if not command:
            return

        self._append_log(f"$ {command}\n")
        self.run_button.setText("停止(&p)")

        try:
            self.process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.project_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=get_user_environment(),
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="ignore",
                close_fds=True,
            )

            def read_output(pipe):
                for line in iter(pipe.readline, ""):
                    self.log_signals.append_log.emit(line)

            threading.Thread(
                target=read_output,
                args=(self.process.stdout,),
                daemon=True
            ).start()

            threading.Thread(
                target=read_output,
                args=(self.process.stderr,),
                daemon=True
            ).start()

            # Start process status checking
            self.status_timer = QTimer()
            self.status_timer.timeout.connect(self._check_process_status)
            self.status_timer.start(100)  # Check every 100ms

        except Exception as e:
            self._append_log(f"运行命令时出错: {str(e)}\n")
            self.run_button.setText("运行(&R)")

    def _submit_feedback(self):
        # 如果是空的话修改为 Continue 提交
        # if self.feedback_text.toPlainText().strip() == "":
        #     self.feedback_text.setText("Continue")
        # self.feedback_result = FeedbackResult(
        #     logs="".join(self.log_buffer),
        #     interactive_feedback=self.feedback_text.toPlainText().strip(),
        #     uploaded_images=self.uploaded_images
        # )
        self.feedback_result = {
            "command_logs": "".join(self.log_buffer),
            "interactive_feedback": self.feedback_text.toPlainText().strip(),
            "uploaded_images": self.uploaded_images
        }
        
        self.close()
        
    def _insert_quick_reply(self, text: str):
        """将预设文本插入到反馈文本框中"""
        self.feedback_text.setText(text)
        self.feedback_text.setFocus()
        # 如果是 开始实施 的话，直接发送反馈
        if text == "开始实施":
            self._submit_feedback()
        
    def _apply_selected_quick_reply(self):
        """应用当前在组合框中选择的快捷回复"""
        selected_text = self.quick_reply_combo.currentText()
        if selected_text:
            self._insert_quick_reply(selected_text)
            
    def _edit_quick_replies(self):
        """打开编辑快捷回复对话框"""
        dialog = QuickReplyEditDialog(self, self.quick_replies)
        if dialog.exec():
            # 用户点击了确定，保存编辑后的快捷回复
            self.quick_replies = dialog.get_quick_replies()
            
            # 更新组合框内容
            self.quick_reply_combo.clear()
            for reply in self.quick_replies:
                self.quick_reply_combo.addItem(reply)
                
            # 保存到设置
            self.settings.beginGroup("MainWindow_General")
            self.settings.setValue("quick_replies", self.quick_replies)
            self.settings.endGroup()
           
    def _upload_image(self):
        """上传图片"""
        # 打开文件选择对话框，限制为图片文件
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.gif *.bmp)"
        )
        if file_path:
            # 复制图片到temp目录
            extension = os.path.splitext(file_path)[1][1:].lower()
            new_filename = generate_random_filename(extension)
            new_filepath = os.path.join(self.temp_dir, new_filename)
            
            # 读取原图片并保存到新位置
            pixmap = QPixmap(file_path)
            pixmap.save(new_filepath)
            
            # 添加到上传列表
            self.uploaded_images.append(new_filepath)
            
            # 更新预览
            self._update_image_preview()
    
    def _get_clipboard_image(self, show_message=True):
        """从剪贴板获取图片
        
        Args:
            show_message: 是否显示消息框提示
            
        Returns:
            bool: 是否成功获取并处理了图片
        """
        clipboard = QApplication.clipboard()
        mime_data = clipboard.mimeData()
        
        if mime_data.hasImage():
            # 从剪贴板获取图片
            image = QImage(clipboard.image())
            if not image.isNull():
                # 保存图片到临时目录
                filename = generate_random_filename()
                filepath = os.path.join(self.temp_dir, filename)
                
                # 保存图片
                if image.save(filepath):
                    self.uploaded_images.append(filepath)
                    self._update_image_preview()
                    if show_message:
                        QMessageBox.information(self, "成功", "已从剪贴板获取图片")
                    return True
                else:
                    if show_message:
                        QMessageBox.warning(self, "错误", "保存图片失败")
                    return False
            else:
                if show_message:
                    QMessageBox.warning(self, "错误", "剪贴板中的图片无效")
                return False
        else:
            if show_message:
                QMessageBox.warning(self, "错误", "剪贴板中没有图片")
            return False
    
    def _update_image_preview(self):
        """更新图片预览区域"""
        # 设置预览区域的可见性：有图片才显示
        self.preview_group.setVisible(bool(self.uploaded_images))
        
        # 清除现有预览
        for widget in self.image_previews:
            widget.deleteLater()
        self.image_previews = []
        self.image_labels = []
        
        # 如果没有图片，显示提示
        if not self.uploaded_images:
            label = QLabel("暂无图片")
            label.setAlignment(Qt.AlignCenter)
            self.preview_grid.addWidget(label, 0, 0)
            self.image_previews.append(label)
            return
        
        # 添加新的预览
        row, col = 0, 0
        max_cols = 3  # 每行最多显示3张图片
        
        for idx, image_path in enumerate(self.uploaded_images):
            # 创建图片容器
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            frame.setFixedSize(100, 130)  # 固定大小
            frame_layout = QVBoxLayout(frame)
            frame_layout.setContentsMargins(5, 5, 5, 5)
            
            # 创建图片标签
            image_label = QLabel()
            image_label.setAlignment(Qt.AlignCenter)
            pixmap = QPixmap(image_path)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(80, 80, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                image_label.setPixmap(pixmap)
                image_label.setToolTip(image_path)
                
                # 设置点击事件
                image_label.mousePressEvent = lambda event, path=image_path: self._preview_image(path)
            else:
                image_label.setText("加载失败")
            
            frame_layout.addWidget(image_label)
            self.image_labels.append(image_label)
            
            # 添加删除按钮
            delete_button = QPushButton("删除")
            delete_button.setProperty("image_index", idx)
            delete_button.clicked.connect(lambda checked, idx=idx: self._delete_image(idx))
            delete_button.setAutoFillBackground(True)  # 设置自动填充背景
            frame_layout.addWidget(delete_button)
            
            # 添加到网格布局
            self.preview_grid.addWidget(frame, row, col)
            self.image_previews.append(frame)
            
            # 更新行列位置
            col += 1
            if col >= max_cols:
                col = 0
                row += 1
    
    def _preview_image(self, image_path):
        """在对话框中预览大图"""
        dialog = QDialog(self)
        dialog.setWindowTitle("图片预览")
        dialog.setMinimumSize(500, 400)
        layout = QVBoxLayout(dialog)
        
        # 创建图片标签
        label = QLabel()
        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            # 缩放图片以适应对话框
            pixmap = pixmap.scaled(480, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            label.setPixmap(pixmap)
            label.setAlignment(Qt.AlignCenter)
        else:
            label.setText("无法加载图片")
        
        layout.addWidget(label)
        
        # 添加关闭按钮
        button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        button_box.accepted.connect(dialog.accept)
        layout.addWidget(button_box)
        
        dialog.exec()
    
    def _delete_image(self, index):
        """删除指定索引的图片"""
        if 0 <= index < len(self.uploaded_images):
            image_path = self.uploaded_images[index]
            
            # 从列表中移除
            self.uploaded_images.pop(index)
            
            # 尝试从磁盘删除
            try:
                os.remove(image_path)
            except Exception as e:
                print(f"删除文件失败: {e}")
            
            # 更新预览
            self._update_image_preview()
            
    def clear_logs(self):
        self.log_buffer = []
        self.log_text.clear()
        
    def _save_config(self):
        # Save run_command and execute_automatically to QSettings under project group
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("run_command", self.config["run_command"])
        self.settings.setValue("execute_automatically", self.config["execute_automatically"])
        self.settings.endGroup()
        
    def _save_window_position(self):
        """保存当前窗口位置到用户设置"""
        # 保存窗口位置到通用设置组
        pos = self.pos()
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("custom_position_x", pos.x())
        self.settings.setValue("custom_position_y", pos.y())
        self.settings.setValue("use_custom_position", True)
        self.settings.endGroup()
        
        # 更新内部状态
        self.use_custom_position = True
        self.custom_position = pos
        
        # 显示状态消息
        self._show_status_message(f"已保存窗口位置 ({pos.x()}, {pos.y()})")

    def closeEvent(self, event):
        # Save general UI settings for the main window (geometry, state)
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        
        # 保存当前窗口实际大小
        self.settings.setValue("custom_width", self.width())
        self.settings.setValue("custom_height", self.height())
        
        # 根据设置决定是否自动保存窗口位置
        auto_save_position = self.settings.value("auto_save_position", True, type=bool)
        if auto_save_position:
            # 自动保存窗口位置
            pos = self.pos()
            self.settings.setValue("custom_position_x", pos.x())
            self.settings.setValue("custom_position_y", pos.y())
            self.settings.setValue("use_custom_position", True)

        # 保存自动提交设置
        self.settings.setValue("auto_submit_enabled", self.auto_submit_enabled)
        self.settings.setValue("auto_submit_wait_time", self.auto_submit_wait_time)

        # 保存窗口置顶设置
        if hasattr(self, 'stay_on_top_check'):
            self.settings.setValue("stay_on_top_enabled", self.stay_on_top_check.isChecked())

        self.settings.endGroup()

        # 保存当前选中的选项卡索引
        self.settings.beginGroup(self.project_group_name)
        self.settings.setValue("selectedTabIndex", self.tab_widget.currentIndex())
        self.settings.endGroup()

        # 自动保存配置，确保"下次自动执行"选项等设置被保存
        self._save_config()

        if self.process:
            kill_tree(self.process)
            
        # 清理未使用的临时图片
        self._cleanup_temp_images()
            
        super().closeEvent(event)

    def _cleanup_temp_images(self):
        """清理临时目录中未使用的图片文件"""
        if not os.path.exists(self.temp_dir):
            return
            
        # 获取当前已上传图片的文件名集合
        uploaded_filenames = set(os.path.basename(path) for path in self.uploaded_images)
        
        # 遍历临时目录中的所有文件
        for filename in os.listdir(self.temp_dir):
            filepath = os.path.join(self.temp_dir, filename)
            # 如果是文件且不在已上传列表中，则删除
            if os.path.isfile(filepath) and filename not in uploaded_filenames:
                try:
                    os.remove(filepath)
                except Exception as e:
                    print(f"清理临时文件失败: {e}")

    def _update_auto_save_position(self, state):
        """更新自动保存窗口位置的设置"""
        is_checked = (state == Qt.Checked)
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("auto_save_position", is_checked)
        self.settings.endGroup()

        # 显示状态更改提示
        status_message = "已启用自动保存窗口位置" if is_checked else "已禁用自动保存窗口位置"
        self._show_status_message(status_message)

    def _update_auto_submit_settings(self):
        """更新自动提交设置"""
        # 更新启用状态
        self.auto_submit_enabled = self.auto_submit_check.isChecked()

        # 更新等待时间
        try:
            wait_time = int(self.auto_submit_time_input.text())
            if wait_time > 0:
                self.auto_submit_wait_time = wait_time
            else:
                # 如果输入无效，恢复默认值
                self.auto_submit_wait_time = 60
                self.auto_submit_time_input.setText("60")
        except ValueError:
            # 如果输入无效，恢复默认值
            self.auto_submit_wait_time = 60
            self.auto_submit_time_input.setText("60")

        # 保存设置
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("auto_submit_enabled", self.auto_submit_enabled)
        self.settings.setValue("auto_submit_wait_time", self.auto_submit_wait_time)
        self.settings.endGroup()

    def _start_auto_submit_countdown(self):
        """启动自动提交倒计时"""
        if not self.auto_submit_enabled:
            return

        # 停止现有的倒计时（如果有）
        self._stop_auto_submit_countdown()

        # 初始化倒计时
        self.countdown_remaining = self.auto_submit_wait_time

        # 创建并启动定时器
        self.auto_submit_timer = QTimer()
        self.auto_submit_timer.timeout.connect(self._update_countdown)
        self.auto_submit_timer.start(1000)  # 每秒触发一次

        # 立即更新一次按钮文本
        self._update_countdown()

    def _stop_auto_submit_countdown(self):
        """停止自动提交倒计时"""
        if self.auto_submit_timer:
            self.auto_submit_timer.stop()
            self.auto_submit_timer = None

        # 恢复按钮原始文本
        if hasattr(self, 'submit_button') and self.original_submit_text:
            self.submit_button.setText(self.original_submit_text)

    def _update_countdown(self):
        """更新倒计时显示"""
        if self.countdown_remaining <= 0:
            # 倒计时结束，自动提交
            self._auto_submit_timeout()
            return

        # 更新按钮文本显示倒计时
        if hasattr(self, 'submit_button'):
            countdown_text = f"{self.original_submit_text} ({self.countdown_remaining}秒)"
            self.submit_button.setText(countdown_text)

        # 减少倒计时
        self.countdown_remaining -= 1

    def _auto_submit_timeout(self):
        """自动提交超时处理"""
        # 停止倒计时
        self._stop_auto_submit_countdown()

        # 执行提交
        self._submit_feedback()

    def _update_stay_on_top_setting(self, state):
        """更新窗口置顶设置"""
        is_checked = (state == Qt.Checked)
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("stay_on_top_enabled", is_checked)
        self.settings.endGroup()

        # 显示状态更改提示
        status_message = "已启用启动时窗口置顶" if is_checked else "已禁用启动时窗口置顶"
        self._show_status_message(status_message)

    def _toggle_stay_on_top(self):
        """切换窗口置顶状态"""
        current_flags = self.windowFlags()
        if current_flags & Qt.WindowStaysOnTopHint:
            # 当前是置顶状态，取消置顶
            new_flags = current_flags & ~Qt.WindowStaysOnTopHint
            self.setWindowFlags(new_flags)
            self._show_status_message("已取消窗口置顶")
        else:
            # 当前不是置顶状态，设置置顶
            new_flags = current_flags | Qt.WindowStaysOnTopHint
            self.setWindowFlags(new_flags)
            self._show_status_message("已设置窗口置顶")

        # 重新显示窗口（setWindowFlags会隐藏窗口）
        self.show()
    
    def _reset_window_position(self):
        """重置窗口位置到屏幕右下角"""
        self.use_custom_position = False
        self.custom_position = None
        
        # 更新设置
        self.settings.beginGroup("MainWindow_General")
        self.settings.setValue("use_custom_position", False)
        self.settings.remove("custom_position_x")
        self.settings.remove("custom_position_y")
        self.settings.endGroup()
        
        # 重新定位窗口
        self._position_window_bottom_right()
        
        # 显示状态消息
        self._show_status_message("已重置窗口位置到屏幕右下角")
        
    def _show_status_message(self, message):
        """显示状态消息"""
        from PySide6.QtWidgets import QLabel
        from PySide6.QtCore import QTimer
        
        status_label = QLabel(message, self)
        status_label.adjustSize()
        
        # 放置在窗口底部中央
        label_x = (self.width() - status_label.width()) // 2
        label_y = self.height() - status_label.height() - 10
        status_label.move(label_x, label_y)
        status_label.show()
        
        # 3秒后自动隐藏
        QTimer.singleShot(3000, status_label.deleteLater)

    def _update_size_info(self):
        """更新大小信息标签"""
        self.size_info_label.setText(f"当前窗口大小: {self.width()} x {self.height()}")

    def _run_git_ai_commit_gui(self):
        """运行 Git AI Commit GUI 工具"""
        # 检查是否已有进程在运行
        if self.process:
            QMessageBox.warning(self, "警告", "已有命令在运行中，请先停止当前命令")
            return

        # 切换到终端选项卡以显示输出
        self.tab_widget.setCurrentIndex(1)  # 终端选项卡是索引1

        # 清空日志缓冲区
        self.log_buffer = []

        command = "uvx git-ai-commit-gui"
        self._append_log(f"$ {command}\n")
        self.git_commit_button.setText("正在运行...")
        self.git_commit_button.setEnabled(False)

        try:
            self.process = subprocess.Popen(
                command,
                shell=True,
                cwd=self.project_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=get_user_environment(),
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="ignore",
                close_fds=True,
            )

            def read_output(pipe):
                for line in iter(pipe.readline, ""):
                    self.log_signals.append_log.emit(line)

            threading.Thread(
                target=read_output,
                args=(self.process.stdout,),
                daemon=True
            ).start()

            threading.Thread(
                target=read_output,
                args=(self.process.stderr,),
                daemon=True
            ).start()

            # 启动进程状态检查，但使用专门的检查方法
            self.git_status_timer = QTimer()
            self.git_status_timer.timeout.connect(self._check_git_process_status)
            self.git_status_timer.start(100)  # 每100毫秒检查一次

        except Exception as e:
            self._append_log(f"运行 Git AI Commit GUI 时出错: {str(e)}\n")
            self.git_commit_button.setText("运行 Git AI Commit GUI")
            self.git_commit_button.setEnabled(True)

    def _check_git_process_status(self):
        """检查 Git AI Commit GUI 进程状态"""
        if self.process and self.process.poll() is not None:
            # 进程已终止
            exit_code = self.process.poll()
            self._append_log(f"\nGit AI Commit GUI 进程已退出，退出代码 {exit_code}\n")
            self.git_commit_button.setText("运行 Git AI Commit GUI")
            self.git_commit_button.setEnabled(True)
            self.process = None

            # 停止状态检查定时器
            if hasattr(self, 'git_status_timer'):
                self.git_status_timer.stop()

    def resizeEvent(self, event):
        """重写resize事件，在窗口大小变化时更新大小信息标签"""
        super().resizeEvent(event)
        # 使用计时器延迟更新，避免频繁更新
        if hasattr(self, 'resize_event_timer'):
            self.resize_event_timer.start(200)  # 200毫秒后更新

    def run(self) -> FeedbackResult:
        self.show()

        # 如果启用自动提交，则启动倒计时
        if self.auto_submit_enabled:
            # 使用QTimer延迟启动，确保窗口完全显示后再开始倒计时
            QTimer.singleShot(500, self._start_auto_submit_countdown)

        QApplication.instance().exec()

        if self.process:
            kill_tree(self.process)

        if not self.feedback_result:
            return FeedbackResult(
                command_logs="".join(self.log_buffer),
                interactive_feedback="",
                uploaded_images=[]
            )

        # 将字典转换为FeedbackResult对象
        if isinstance(self.feedback_result, dict):
            return FeedbackResult(
                command_logs=self.feedback_result.get("command_logs", ""),
                interactive_feedback=self.feedback_result.get("interactive_feedback", ""),
                uploaded_images=self.feedback_result.get("uploaded_images", [])
            )

        return self.feedback_result

def get_project_settings_group(project_dir: str) -> str:
    # Create a safe, unique group name from the project directory path
    # Using only the last component + hash of full path to keep it somewhat readable but unique
    basename = os.path.basename(os.path.normpath(project_dir))
    full_hash = hashlib.md5(project_dir.encode('utf-8')).hexdigest()[:8]
    return f"{basename}_{full_hash}"

def feedback_ui(project_directory: str, prompt: str, output_file: Optional[str] = None) -> Optional[FeedbackResult]:
    app = QApplication.instance() or QApplication()
    
    # 在提示文本中添加AI助手信息
    ai_prompt = f"AI助手: {prompt}"
    
    ui = FeedbackUI(project_directory, ai_prompt)
    result = ui.run()

    if output_file and result:
        # Ensure the directory exists
        os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".", exist_ok=True)
        # Save the result to the output file
        with open(output_file, "w") as f:
            json.dump(result, f)
        return None

    return result

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行反馈用户界面")
    parser.add_argument("--project-directory", default=os.getcwd(), help="The project directory to run the command in")
    parser.add_argument("--prompt", default="I implemented the changes you requested.", help="The prompt to show to the user")
    parser.add_argument("--output-file", help="Path to save the feedback result as JSON")
    args = parser.parse_args()

    result = feedback_ui(args.project_directory, args.prompt, args.output_file)
    if result:
        feedback_result = {
            "command_logs": result['command_logs'],
            "interactive_feedback": result['interactive_feedback'],
            "uploaded_images": result['uploaded_images']
        }
        print(json.dumps(feedback_result, indent=4))
    sys.exit(0)
