# -*- coding: utf-8 -*-
"""通用任务队列：视频/照片两个工具共用的任务模型、worker 循环与任务路由。

每个工具实例化一个 TaskManager（各自的任务表、队列、并发数、输出路径预留），
实际编码逻辑由工具模块实现并以 run_task(mgr, t) 注入。
"""
import logging
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from flask import jsonify, send_file

from web_common import open_in_explorer

log = logging.getLogger("media.tasks")

COMMON_KEYS = ("id", "name", "source", "params", "status", "progress",
               "error", "output", "src_size", "out_size", "created",
               "started", "finished")


class TaskManager:
    """一个工具一个实例。

    run_task(mgr, t)      实际编码 + 状态收尾（完成后/失败/取消的落账）；
    candidate(src, name, params)  输出路径候选（不含 _1/_2 去重）；
    serialize_keys        序列化时在公共字段外附加的任务字段；
    on_cancel_running(t)  取消运行中任务的钩子（如向 ffmpeg stdin 发 q）。
    """

    def __init__(self, name, workers, run_task, candidate,
                 serialize_keys=(), on_cancel_running=None):
        self.name = name
        self.workers = workers
        self.run_task = run_task
        self.candidate = candidate
        self.serialize_keys = COMMON_KEYS + tuple(serialize_keys)
        self.on_cancel_running = on_cancel_running
        self.tasks = OrderedDict()
        self.queue = []
        self.lock = threading.Lock()
        self.wake = threading.Event()
        # 已分配但尚未开始/完成的输出路径，避免并行任务分到同一个输出文件名
        self.reserved_outputs = set()

    # ---------------------------------------------------------- task model
    def serialize(self, t):
        return {k: t.get(k) for k in self.serialize_keys}

    def new_task(self, source, name, params):
        tid = uuid.uuid4().hex[:12]
        out = self.reserve_output(source, name, params)
        task = {
            "id": tid,
            "source": str(source),
            "name": name,
            "params": params,
            "status": "queued",     # queued / running / completed / failed / cancelled / cancelling
            "progress": 0.0,
            "speed": None,
            "error": "",
            "output": str(out),
            "cmd": "",
            "duration": None,
            "src_size": 0,
            "out_size": 0,
            "created": time.time(),
            "started": None,
            "finished": None,
        }
        with self.lock:
            self.tasks[tid] = task
            self.queue.append(tid)
        self.wake.set()
        return task

    def fail_task(self, t, msg):
        t["status"] = "failed"
        t["error"] = msg
        t["finished"] = time.time()

    # ---------------------------------------------------- output reservation
    def _advance_unique(self, p):
        i = 1
        while p.exists() or str(p) in self.reserved_outputs:
            p = p.with_name(f"{p.stem}_{i}{p.suffix}")
            i += 1
        return p

    def reserve_output(self, src, name, params):
        """入队时锁定输出路径，避免并行任务分到同一个文件名互相覆盖。"""
        with self.lock:
            p = self._advance_unique(Path(self.candidate(src, name, params)))
            self.reserved_outputs.add(str(p))
        return p

    def preview_output(self, candidate):
        """预览用：避开现有文件与已预留名给出唯一路径，但不预留。"""
        p = Path(candidate)
        i = 1
        with self.lock:
            reserved = set(self.reserved_outputs)
        while p.exists() or str(p) in reserved:
            p = p.with_name(f"{p.stem}_{i}{p.suffix}")
            i += 1
        return p

    def release_output(self, path):
        with self.lock:
            self.reserved_outputs.discard(str(path))

    # ------------------------------------------------------------- control
    def stop_all(self):
        """运行中 → cancelling，排队 → cancelled；返回需要执行取消钩子的任务。

        注意：此处已持有 self.lock，不能调用会再次加锁的 release_output()。
        """
        cancels = []
        with self.lock:
            for t in self.tasks.values():
                st = t["status"]
                if st == "running":
                    t["status"] = "cancelling"
                    cancels.append(t)
                elif st == "queued":
                    t["status"] = "cancelled"
                    t["error"] = "用户取消"
                    t["finished"] = time.time()
                    self.reserved_outputs.discard(str(t.get("output", "")))
        return cancels

    # --------------------------------------------------------- worker loop
    def worker(self):
        while True:
            self.wake.clear()
            with self.lock:
                tid = next((qid for qid in self.queue
                            if self.tasks.get(qid, {}).get("status") == "queued"), None)
            if tid is None:
                self.wake.wait(timeout=1.0)
                continue
            t = self.tasks.get(tid)
            if not t:
                continue
            with self.lock:
                t["status"] = "running"
                t["started"] = time.time()
            log.info("[%s] 任务开始 %s <- %s", self.name, t["name"], t["source"])
            try:
                self.run_task(self, t)
                log.info("[%s] 任务结束 %s [%s]", self.name, t["name"], t["status"])
            except Exception as e:
                log.exception("[%s] 任务异常 %s", self.name, t["name"])
                with self.lock:
                    self.fail_task(t, str(e))
            finally:
                self.release_output(t.get("output", ""))


def register_task_routes(app, mgr, prefix):
    """为某个工具注册任务相关路由：列表/详情/取消/重试/清空/停止全部/下载/定位。"""

    def _route(rule, **options):
        def deco(fn):
            # 两个工具注册同名视图函数，endpoint 用工具名前缀避免冲突
            app.add_url_rule(prefix + rule, f"{mgr.name}_{fn.__name__}", fn, **options)
            return fn
        return deco

    @_route("/tasks")
    def list_tasks():
        with mgr.lock:
            items = [mgr.serialize(t) for t in mgr.tasks.values()]
        return jsonify({"tasks": items})

    @_route("/tasks/<tid>")
    def get_task(tid):
        with mgr.lock:
            t = mgr.tasks.get(tid)
            data = mgr.serialize(t) if t else None
        if not data:
            return jsonify({"error": "not found"}), 404
        return jsonify(data)

    @_route("/tasks/<tid>/cancel", methods=["POST"])
    def cancel(tid):
        with mgr.lock:
            t = mgr.tasks.get(tid)
            if not t:
                return jsonify({"error": "not found"}), 404
            st = t["status"]
            if st == "running":
                t["status"] = "cancelling"
            elif st == "queued":
                t["status"] = "cancelled"
                t["error"] = "用户取消"
                t["finished"] = time.time()
        if st == "running":
            if mgr.on_cancel_running:
                mgr.on_cancel_running(t)
        elif st == "queued":
            mgr.release_output(t.get("output", ""))
        return jsonify({"ok": True})

    @_route("/tasks/<tid>/retry", methods=["POST"])
    def retry(tid):
        with mgr.lock:
            t = mgr.tasks.get(tid)
            st = t["status"] if t else None
        if not t:
            return jsonify({"error": "not found"}), 404
        if st not in ("failed", "cancelled"):
            return jsonify({"error": "只能重试失败或已取消的任务"}), 400
        src = Path(t["source"])
        if not src.exists():
            return jsonify({"error": "源文件不存在或已被删除"}), 400
        nt = mgr.new_task(src, t["name"], dict(t["params"]))
        log.info("[%s] 重试任务 %s -> %s", mgr.name, tid, nt["id"])
        return jsonify(mgr.serialize(nt))

    @_route("/tasks/clear", methods=["POST"])
    def clear():
        with mgr.lock:
            for tid in list(mgr.tasks.keys()):
                if mgr.tasks[tid]["status"] in ("completed", "failed", "cancelled"):
                    del mgr.tasks[tid]
            mgr.queue[:] = [tid for tid in mgr.queue if tid in mgr.tasks]
        return jsonify({"ok": True})

    @_route("/stop-all", methods=["POST"])
    def stop_all():
        cancels = mgr.stop_all()
        if mgr.on_cancel_running:
            for t in cancels:
                mgr.on_cancel_running(t)
        return jsonify({"ok": True})

    @_route("/download/<tid>")
    def download(tid):
        with mgr.lock:
            t = mgr.tasks.get(tid)
            out = t.get("output") if t else None
        if not out or not Path(out).exists():
            return jsonify({"error": "文件不存在"}), 404
        return send_file(Path(out), as_attachment=True)

    @_route("/open-file/<tid>", methods=["POST"])
    def open_file(tid):
        with mgr.lock:
            t = mgr.tasks.get(tid)
            out = t.get("output") if t else None
        if not out or not Path(out).exists():
            return jsonify({"error": "文件不存在"}), 404
        try:
            open_in_explorer(Path(out))
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
