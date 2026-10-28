"""WorkBuddy Switch 入口：开 DPI 感知，建主窗口，进事件循环。"""
import tkinter as tk

from window import Window


def main():
    """创建主窗口并进入 tkinter 主循环。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    Window(root)
    root.mainloop()


if __name__ == "__main__":
    main()
