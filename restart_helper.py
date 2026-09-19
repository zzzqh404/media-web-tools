# -*- coding: utf-8 -*-
"""服务重启助手：由 app.py 以独立进程调起，杀掉旧服务与残留 ffmpeg 后重启。"""
import os
import signal
import subprocess
import sys
import time

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000


def kill_pid(pid):
    if IS_WIN:
        try:
            # 只杀指定 PID，不用 /IM，避免误杀无关的 ffmpeg 进程
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           creationflags=CREATE_NO_WINDOW, capture_output=True)
        except Exception:
            pass
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
                return
            except ProcessLookupError:
                return
            except Exception:
                continue


def main():
    argv = sys.argv[1:]
    if len(argv) < 2:
        return
    pid = int(argv[0])
    cwd = argv[1]
    ff_pids = []
    for x in argv[2:]:
        try:
            ff_pids.append(int(x))
        except ValueError:
            pass

    time.sleep(1.5)
    # helper 是独立进程，杀服务进程不会影响自己
    kill_pid(pid)
    time.sleep(0.5)
    # 清理该服务启动、可能残留的 ffmpeg 子进程（只杀传入的 PID）
    for p in ff_pids:
        kill_pid(p)
    time.sleep(0.3)

    kwargs = {"cwd": cwd, "close_fds": True}
    if IS_WIN:
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | CREATE_NO_WINDOW)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, "app.py"], **kwargs)


if __name__ == "__main__":
    main()
