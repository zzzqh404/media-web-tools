# -*- coding: utf-8 -*-
"""tkinter 文件对话框子进程：由 app.py 调起，把用户选中的路径回传给浏览器。

用法: dialog_helper.py <files|dir> <类型标签> <文件对话框标题> <文件夹对话框标题> [扩展名...]
"""
import json
import sys


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = sys.argv[1:]
    kind = argv[0] if argv else "files"
    label = argv[1] if len(argv) > 1 else "文件"
    title_files = argv[2] if len(argv) > 2 else "选择文件"
    title_dir = argv[3] if len(argv) > 3 else "选择文件夹"
    try:
        from tkinter import Tk, filedialog
    except Exception as e:
        print(json.dumps({"error": f"无法打开图形对话框（未安装 tkinter）: {e}"},
                         ensure_ascii=False))
        return
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "dir":
            path = filedialog.askdirectory(title=title_dir)
            print(json.dumps({"path": path or ""}, ensure_ascii=False))
        else:
            pat = " ".join("*" + e for e in argv[4:])
            paths = filedialog.askopenfilenames(
                title=title_files,
                filetypes=[(label, pat), ("所有文件", "*.*")])
            print(json.dumps({"paths": list(paths)}, ensure_ascii=False))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
