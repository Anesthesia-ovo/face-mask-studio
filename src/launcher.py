"""Windows GUI entry point. No telemetry, updates or runtime downloads."""
import sys
from pathlib import Path

import offline

offline.enable()


def main():
    if "--self-test" in sys.argv:
        from selfcheck import run
        position = sys.argv.index("--self-test")
        report_path = Path(sys.argv[position + 1])
        result = run(report_path)
        return 0 if result["passed"] else 1
    try:
        from app import main as launch_ui
        launch_ui()
        return 0
    except Exception as error:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("人脸隐私遮挡工具", "启动失败：\n" + str(error) + "\n\n请保留 EXE 同目录下的 _internal 文件夹。")
        root.destroy()
        return 1


if __name__ == "__main__":
    sys.exit(main())

