# -*- coding: utf-8 -*-
"""
定时开关机助手
- 定时关机 / 休眠：通过 Windows 计划任务执行 shutdown 命令，
  并在执行前 1 分钟由计划任务拉起提醒弹窗（可延迟 15 分钟 / 取消关机）
- 定时唤醒：通过带「唤醒计算机运行此任务」标志的计划任务，把电脑从睡眠/休眠中唤醒
所有任务完全由 Windows 任务计划程序执行，关闭本程序后依然有效。
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import customtkinter as ctk

# ---------------------------------------------------------------- 常量与配置

APP_NAME = "定时开关机助手"
APP_VERSION = "1.1.1"

TASK_SHUTDOWN = "PowerScheduler_AutoShutdown"
TASK_SHUTDOWN_WARN = "PowerScheduler_ShutdownWarn"   # 关机前 1 分钟提醒弹窗
TASK_SHUTDOWN_DELAY = "PowerScheduler_DelayShutdown"  # 延迟关机的一次性任务
TASK_WAKE = "PowerScheduler_AutoWake"

WARN_LEAD_SECONDS = 60      # 提醒提前量
DELAY_MINUTES = 15          # 弹窗「延迟」按钮的延迟时长

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PREFS_PATH = os.path.join(APP_DIR, "prefs.json")
EVENT_PATH = os.path.join(tempfile.gettempdir(), "PowerScheduler_events.jsonl")

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
DOW_DAILY = {0, 1, 2, 3, 4}  # 工作日

C = {
    "bg": "#12141d",
    "card": "#1a1d2b",
    "border": "#2a2e42",
    "entry": "#222636",
    "entry_hover": "#2a2f44",
    "text": "#eceef8",
    "sub": "#8b90a6",
    "faint": "#5d6274",
    "red": "#ff5d5d",
    "red_hover": "#e34b4b",
    "red_dim": "#3a2028",
    "red_text": "#ffffff",
    "amber": "#ffb454",
    "amber_hover": "#eda03c",
    "amber_dim": "#3a2f1e",
    "amber_text": "#241a08",
    "green": "#3ddc84",
    "green_dim": "#1d3226",
    "gray_dim": "#262a3a",
    "titlebar": "#15171f",
}

F_TITLE = ("Microsoft YaHei UI", 21, "bold")
F_SUB = ("Microsoft YaHei UI", 12)
F_CLOCK = ("Microsoft YaHei UI", 26, "bold")
F_CARD = ("Microsoft YaHei UI", 17, "bold")
F = ("Microsoft YaHei UI", 13)
F_BTN = ("Microsoft YaHei UI", 13, "bold")
F_SMALL = ("Microsoft YaHei UI", 11)
F_TINY = ("Microsoft YaHei UI", 10)
F_MONO = ("Consolas", 12, "bold")
F_HUGE = ("Consolas", 44, "bold")

CREATE_NO_WINDOW = 0x08000000

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{sid}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Triggers>{trigger}</Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>{start_when_available}</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <WakeToRun>{wake}</WakeToRun>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>"""

TRIGGER_ONCE = """<TimeTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>"""

TRIGGER_DAILY = """<CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""

TRIGGER_WEEKLY = """<CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>{days}</DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>"""

DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ---------------------------------------------------------------- 后台逻辑


def run_cmd(args) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)


def decode_console(b: bytes) -> str:
    if not b:
        return ""
    for enc in ("gbk", "utf-8"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace")


def decode_xml(b: bytes) -> str:
    """schtasks /xml 的输出编码不稳定：可能带 BOM 的 UTF-16，也可能实际是 UTF-8。
    注意 utf-16 对任意偶数长度字节都能「成功」解码出乱码，必须先看 BOM/内容。"""
    if not b:
        return ""
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16")
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            pass
    return b.decode("utf-16", "replace")


def is_admin() -> bool:
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def current_sid() -> str:
    r = run_cmd(["whoami", "/user", "/fo", "csv", "/nh"])
    m = re.search(r"S-1-5-[\d-]+", decode_console(r.stdout))
    return m.group() if m else ""


def hibernate_available() -> bool:
    """powercfg /a 的「可用」段落里是否出现 休眠/Hibernate。"""
    r = run_cmd(["powercfg", "/a"])
    txt = decode_console(r.stdout)
    if not txt:
        return False
    parts = re.split(r"此系统上没有以下睡眠状态|The following sleep states are not available", txt, maxsplit=1)
    avail = parts[0]
    return ("休眠" in avail) or ("Hibernate" in avail)


def enable_hibernate() -> tuple[bool, str]:
    r = run_cmd(["powercfg", "/h", "on"])
    return (r.returncode == 0), decode_console(r.stderr) or decode_console(r.stdout)


def resource_path(name: str) -> str:
    """兼容 PyInstaller onefile：优先从解包目录取资源。"""
    base = getattr(sys, "_MEIPASS", None) or APP_DIR
    return os.path.join(base, name)


def _build_trigger(mode: str, start_dt: dt.datetime, dows: set[int] | None) -> str:
    iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
    if mode == "once":
        return TRIGGER_ONCE.format(start=iso)
    if mode == "daily":
        return TRIGGER_DAILY.format(start=iso)
    days = "".join(f"<{DOW_NAMES[d]}/>" for d in sorted(dows or set()))
    return TRIGGER_WEEKLY.format(start=iso, days=days)


def register_task(taskname: str, mode: str, start_dt: dt.datetime,
                  dows: set[int] | None, command: str, arguments: str,
                  wake: bool, description: str,
                  start_when_available: bool = False) -> tuple[bool, str]:
    """创建/覆盖一个计划任务。mode: once | daily | workday(=每周一~五) | weekly"""
    trigger_mode = mode
    if mode == "workday":
        trigger_mode, dows = "weekly", DOW_DAILY
    xml = TASK_XML.format(
        description=escape(description),
        sid=escape(current_sid()),
        trigger=_build_trigger(trigger_mode, start_dt, dows),
        wake="true" if wake else "false",
        start_when_available="true" if start_when_available else "false",
        command=escape(command),
        arguments=escape(arguments),
    )
    os.makedirs(APP_DIR, exist_ok=True)
    xml_path = os.path.join(APP_DIR, f"_{taskname}.xml")
    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml)
    try:
        r = run_cmd(["schtasks", "/create", "/tn", taskname, "/xml", xml_path, "/f"])
        if r.returncode != 0:
            return False, decode_console(r.stderr).strip() or f"schtasks 退出码 {r.returncode}"
        return True, ""
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass


def delete_task(taskname: str) -> bool:
    r = run_cmd(["schtasks", "/delete", "/tn", taskname, "/f"])
    return r.returncode == 0


def parse_task_xml(text: str) -> dict | None:
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ET.fromstring(text)

    def setting(tag: str, default: bool) -> bool:
        """导出的 XML 会省略取默认值的开关，缺省时 Enabled=true / WakeToRun=false。"""
        el = root.find(f".//t:Settings/t:{tag}", ns)
        if el is None or not el.text:
            return default
        return el.text.strip().lower() == "true"

    info = {
        "enabled": setting("Enabled", True),
        "wake": setting("WakeToRun", False),
        "mode": "once", "start": None, "hh": 0, "mm": 0, "dows": set(),
        "command": "", "arguments": "",
    }
    cmd = root.find(".//t:Actions/t:Exec/t:Command", ns)
    args = root.find(".//t:Actions/t:Exec/t:Arguments", ns)
    info["command"] = (cmd.text or "") if cmd is not None else ""
    info["arguments"] = (args.text or "") if args is not None else ""

    trig = root.find(".//t:Triggers", ns)
    if trig is None or len(trig) == 0:
        return None
    node = list(trig)[0]
    sb = node.find("t:StartBoundary", ns)
    info["start"] = dt.datetime.fromisoformat(sb.text) if (sb is not None and sb.text) else None
    if node.tag.endswith("TimeTrigger"):
        info["mode"] = "once"
    elif node.find(".//t:ScheduleByDay", ns) is not None:
        info["mode"] = "daily"
    else:
        info["mode"] = "weekly"
        w = node.find(".//t:DaysOfWeek", ns)
        if w is not None:
            info["dows"] = {i for i, name in enumerate(DOW_NAMES)
                            if any(ch.tag.endswith(name) for ch in w)}
    sbt = info["start"] or dt.datetime.now()
    info["hh"], info["mm"] = sbt.hour, sbt.minute
    return info


def query_task(taskname: str) -> dict | None:
    r = run_cmd(["schtasks", "/query", "/tn", taskname, "/xml"])
    if r.returncode != 0:
        return None
    try:
        return parse_task_xml(decode_xml(r.stdout))
    except ET.ParseError:
        return None


def next_run_of(info: dict | None, now: dt.datetime) -> dt.datetime | None:
    """根据已解析的任务信息推算下次执行时间。"""
    if not info or not info["enabled"]:
        return None
    hh, mm = info["hh"], info["mm"]
    start = info["start"]

    def at(day: dt.date) -> dt.datetime:
        return dt.datetime.combine(day, dt.time(hh, mm))

    if info["mode"] == "once":
        return start if (start and start > now) else None

    if info["mode"] == "daily":
        d = now.date()
        cand = at(d)
        if cand <= now:
            d += dt.timedelta(days=1)
            cand = at(d)
        if start and cand < start:
            cand = max(start, at(start.date()) if at(start.date()) > now else at(start.date()) + dt.timedelta(days=1))
        return cand

    dows = info["dows"] or set()
    d = now.date()
    cand = at(d)
    if cand <= now:
        d += dt.timedelta(days=1)
        cand = at(d)
    for _ in range(8):
        if d.weekday() in dows and (not start or cand >= start):
            return cand
        d += dt.timedelta(days=1)
        cand = at(d)
    return None


def next_occurrence(mode: str, hh: int, mm: int, now: dt.datetime) -> dt.datetime:
    """今天/明天的下一次 HH:MM。"""
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if t <= now:
        t += dt.timedelta(days=1)
    return t


def humanize_time(t: dt.datetime, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()
    days = (t.date() - now.date()).days
    hm = f"{t.hour:02d}:{t.minute:02d}"
    if days == 0:
        return ("今晚 " if t.hour >= 18 else "今天 ") + hm
    if days == 1:
        return f"明天 {hm}"
    return f"{t.month}月{t.day}日({WEEKDAY_CN[t.weekday()]}) {hm}"


def fmt_delta(td: dt.timedelta) -> str:
    s = max(0, int(td.total_seconds()))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h >= 24:
        return f"{h // 24}天 {h % 24:02d}:{m:02d}:{sec:02d}"
    return f"{h:02d}:{m:02d}:{sec:02d}"


def record_warn_event(action: str, success: bool, message: str, verb: str) -> None:
    """让独立运行的提醒窗口把操作结果同步给主面板。"""
    event = {"action": action, "success": success, "message": message,
             "verb": verb, "time": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        with open(EVENT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass


def describe_schedule(info: dict) -> str:
    mode = info["mode"]
    hm = f"{info['hh']:02d}:{info['mm']:02d}"
    if mode == "once":
        return f"一次性 {hm}"
    if mode == "daily":
        return f"每天 {hm}"
    dows = info.get("dows") or set()
    if dows == DOW_DAILY:
        return f"工作日 {hm}"
    return "每周 " + "、".join(WEEKDAY_CN[d] for d in sorted(dows)) + " " + hm


# ---------------------------------------------------------- 关机任务组管理


def get_warn_launcher() -> tuple[str, str]:
    """提醒弹窗的启动命令：打包后=exe 本身；源码运行=pythonw + 脚本。"""
    if getattr(sys, "frozen", False):
        return sys.executable, "--warn"
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return exe, f'"{os.path.abspath(__file__)}" --warn'


def shutdown_args(action_verb: str, force: bool) -> str:
    return "/h" if action_verb == "休眠" else ("/s /t 0" + (" /f" if force else ""))


def _register_warn_task(mode: str, warn_start: dt.datetime, dows: set[int] | None) -> bool:
    cmd, arguments = get_warn_launcher()
    ok, _err = register_task(
        TASK_SHUTDOWN_WARN, mode, warn_start, dows,
        command=cmd, arguments=arguments, wake=False,
        description=f"{APP_NAME} 关机前提醒弹窗",
        start_when_available=True)
    return ok


def create_shutdown_task(mode: str, hh: int, mm: int, force: bool,
                         action_verb: str) -> tuple[bool, str, bool]:
    """创建定时关机主任务 + 前 1 分钟提醒任务。
    返回 (主任务是否成功, 错误信息, 提醒任务是否设置)。"""
    now = dt.datetime.now()
    start = next_occurrence(mode, hh, mm, now)
    dows = DOW_DAILY if mode == "workday" else None

    delete_task(TASK_SHUTDOWN_DELAY)  # 清掉可能遗留的延迟任务
    ok, err = register_task(
        TASK_SHUTDOWN, mode, start, dows,
        command="shutdown", arguments=shutdown_args(action_verb, force),
        wake=False, description=f"{APP_NAME} 定时{action_verb}")
    if not ok:
        return False, err, False

    warn_start = start - dt.timedelta(seconds=WARN_LEAD_SECONDS)
    if mode == "once":
        # Preserve seconds so a warning created mid-minute still has a future trigger.
        warn_start = max(warn_start, now + dt.timedelta(seconds=2))
        warn_ok = _register_warn_task("once", warn_start, None)
    else:
        warn_start = max(warn_start, now + dt.timedelta(seconds=2))
        warn_ok = _register_warn_task(mode, warn_start, dows)
    return True, "", warn_ok


def delete_shutdown_tasks() -> None:
    for name in (TASK_SHUTDOWN, TASK_SHUTDOWN_WARN, TASK_SHUTDOWN_DELAY):
        delete_task(name)


def delay_shutdown(minutes: int = DELAY_MINUTES) -> tuple[bool, str]:
    """把即将执行的关机推迟 N 分钟。
    一次性任务：整体改期重建；循环任务：暂停本次，建一次性延迟任务执行后自动恢复。"""
    now = dt.datetime.now()
    info = query_task(TASK_SHUTDOWN)
    if not info:
        return False, "定时关机任务不存在"
    args = info["arguments"] or "/s /t 0"
    verb = "休眠" if args.strip().startswith("/h") else "关机"

    if info["mode"] == "once":
        new_start = (info["start"] or now) + dt.timedelta(minutes=minutes)
        delete_shutdown_tasks()
        ok, err = register_task(
            TASK_SHUTDOWN, "once", new_start, None,
            command="shutdown", arguments=args, wake=False,
            description=f"{APP_NAME} 定时{verb}")
        if not ok:
            return False, err
        warn_start = new_start - dt.timedelta(seconds=WARN_LEAD_SECONDS)
        if warn_start > now + dt.timedelta(seconds=15):
            _register_warn_task("once", warn_start, None)
        return True, humanize_time(new_start, now)

    # 循环任务：暂停主任务，由一次性延迟任务在到点时恢复并执行关机
    r = run_cmd(["schtasks", "/change", "/tn", TASK_SHUTDOWN, "/disable"])
    if r.returncode != 0:
        return False, "暂停主任务失败"
    new_start = now + dt.timedelta(minutes=minutes)
    chain = (f'/c schtasks /change /tn {TASK_SHUTDOWN} /enable '
             f'& shutdown {args}')
    ok, err = register_task(
        TASK_SHUTDOWN_DELAY, "once", new_start, None,
        command="cmd.exe", arguments=chain, wake=False,
        description=f"{APP_NAME} 延迟{verb}：恢复主任务并执行")
    if not ok:
        run_cmd(["schtasks", "/change", "/tn", TASK_SHUTDOWN, "/enable"])
        return False, err
    return True, humanize_time(new_start, now)


def startup_maintenance() -> list[str]:
    """启动时：清理过期任务、修复配对、恢复被延迟暂停的任务。"""
    msgs: list[str] = []
    now = dt.datetime.now()
    for name in (TASK_SHUTDOWN, TASK_WAKE, TASK_SHUTDOWN_WARN, TASK_SHUTDOWN_DELAY):
        info = query_task(name)
        if info and info["mode"] == "once" and info["start"] and info["start"] <= now:
            delete_task(name)
            msgs.append(f"已清理过期任务：{name}")
            if name == TASK_SHUTDOWN_DELAY:
                main = query_task(TASK_SHUTDOWN)
                if main and not main["enabled"]:
                    run_cmd(["schtasks", "/change", "/tn", TASK_SHUTDOWN, "/enable"])
                    msgs.append("已恢复被延迟暂停的定时关机任务")

    main = query_task(TASK_SHUTDOWN)
    warn = query_task(TASK_SHUTDOWN_WARN)
    if main is None:
        if warn:
            delete_task(TASK_SHUTDOWN_WARN)
            msgs.append("已清理孤立的提醒任务")
    else:
        nxt = next_run_of(main, now)
        if warn is None and nxt and (nxt - now).total_seconds() > WARN_LEAD_SECONDS + 30:
            dows = main.get("dows") or None
            mode = main["mode"]
            warn_start = nxt - dt.timedelta(seconds=WARN_LEAD_SECONDS)
            if mode != "once" and warn_start <= now:
                warn_start += dt.timedelta(days=1)
            if _register_warn_task(mode, warn_start, dows):
                msgs.append("已为现有定时关机任务补充执行前提醒")
    return msgs


# ---------------------------------------------------------------- 模态对话框


class ConfirmDialog(ctk.CTkToplevel):
    """统一的确认 / 提示弹窗。on_confirm 回调在点「确认」后触发。"""

    def __init__(self, master, title, message, confirm_text="确 认",
                 danger=True, on_confirm=None, show_cancel=True):
        super().__init__(master, fg_color=C["card"])
        self.on_confirm = on_confirm
        self.withdraw()
        self.title(title)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)

        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=28, pady=(24, 20))
        bar = ctk.CTkFrame(wrap, fg_color=C["red"] if danger else C["amber"],
                           corner_radius=3, height=4)
        bar.pack(fill="x", pady=(0, 14))
        msg = ctk.CTkLabel(wrap, text=message, font=F, text_color=C["text"],
                           justify="left", wraplength=380)
        msg.pack(anchor="w")

        btns = ctk.CTkFrame(wrap, fg_color="transparent")
        btns.pack(fill="x", pady=(20, 0))
        cancel_text = "取 消" if show_cancel else "关 闭"
        ctk.CTkButton(btns, text=cancel_text, width=110, height=36, font=F_BTN,
                      fg_color=C["entry"], hover_color=C["entry_hover"],
                      text_color=C["sub"], corner_radius=9,
                      command=self._close).pack(side="right")
        if show_cancel and on_confirm:
            ctk.CTkButton(btns, text=confirm_text, width=110, height=36, font=F_BTN,
                          fg_color=C["red"] if danger else C["amber"],
                          hover_color=C["red_hover"] if danger else C["amber_hover"],
                          text_color=C["red_text"] if danger else C["amber_text"],
                          corner_radius=9, command=self._confirm).pack(side="right", padx=(0, 10))

        self.after(60, self._show)
        # 键盘操作：回车确认，Esc 取消
        self.bind("<Return>", lambda e: self._confirm())
        self.bind("<Escape>", lambda e: self._close())

    def _show(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2 - 40}")
        self.deiconify()
        self.lift()
        self.focus_force()
        self.grab_set()

    def _confirm(self):
        self.grab_release()
        self.destroy()
        if self.on_confirm:
            self.on_confirm()

    def _close(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------- 任务卡片


class TimeStepper(ctk.CTkFrame):
    def __init__(self, master, maximum: int, initial: int):
        super().__init__(master, fg_color="transparent")
        self.maximum = maximum
        self.value = tk.StringVar(value=f"{initial:02d}")
        self._validate = self.register(self._is_valid)

        self.entry = ctk.CTkEntry(
            self, width=48, height=36, textvariable=self.value,
            font=F_MONO, justify="center", corner_radius=8,
            fg_color=C["entry"], border_color=C["border"],
            text_color=C["text"], validate="key",
            validatecommand=(self._validate, "%P"))
        self.entry.pack(side="left", fill="y")

        controls = ctk.CTkFrame(self, fg_color="transparent")
        controls.pack(side="left", padx=(3, 0), fill="y")
        for symbol, delta in (("▲", 1), ("▼", -1)):
            ctk.CTkButton(
                controls, text=symbol, width=20, height=16,
                font=("Microsoft YaHei UI", 9), corner_radius=4,
                fg_color=C["entry_hover"], hover_color=C["border"],
                text_color=C["sub"], command=lambda d=delta: self.step(d)
            ).pack(pady=1)

        self.entry.bind("<Up>", lambda _event: self.step(1))
        self.entry.bind("<Down>", lambda _event: self.step(-1))
        self.entry.bind("<MouseWheel>", self._on_wheel)
        self.entry.bind("<FocusOut>", self._normalize)

    def _is_valid(self, value: str) -> bool:
        if value == "":
            return True
        return value.isdigit() and len(value) <= 2 and int(value) <= self.maximum

    def _normalize(self, _event=None):
        value = self.value.get()
        number = min(self.maximum, max(0, int(value or 0)))
        self.value.set(f"{number:02d}")

    def _on_wheel(self, event):
        self.step(1 if event.delta > 0 else -1)
        return "break"

    def step(self, delta: int):
        value = int(self.value.get() or 0)
        self.value.set(f"{(value + delta) % (self.maximum + 1):02d}")

    def get(self) -> str:
        return self.value.get()

    def set(self, value: str):
        number = min(self.maximum, max(0, int(value)))
        self.value.set(f"{number:02d}")


class PowerCard(ctk.CTkFrame):
    """定时任务卡片：模式 / 时间 / 操作 / 状态。"""

    def __init__(self, master, title, icon, accent, accent_hover, accent_dim,
                 accent_text, placeholder, **kw):
        super().__init__(master, fg_color=C["card"], corner_radius=18,
                         border_width=1, border_color=C["border"], **kw)
        self.accent, self.accent_hover = accent, accent_hover
        self.accent_dim, self.accent_text = accent_dim, accent_text
        self.placeholder = placeholder
        self.info: dict | None = None
        self.busy = False

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)  # 弹性空隙：拉伸时按钮/状态贴底
        pad = dict(padx=20)

        # --- 标题行
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 4))
        icon_lbl = ctk.CTkLabel(head, text=icon, width=44, height=44,
                                font=("Segoe UI Emoji", 20), fg_color=accent_dim,
                                corner_radius=14, text_color=accent)
        icon_lbl.pack(side="left")
        ttl = ctk.CTkFrame(head, fg_color="transparent")
        ttl.pack(side="left", padx=12)
        ctk.CTkLabel(ttl, text=title, font=F_CARD, text_color=C["text"],
                     anchor="w").pack(anchor="w")
        self.head_sub = ctk.CTkLabel(ttl, text=placeholder, font=F_TINY,
                                     text_color=C["faint"], anchor="w")
        self.head_sub.pack(anchor="w")
        self.dot = ctk.CTkLabel(head, text="●", font=("Microsoft YaHei UI", 14),
                                text_color=C["faint"])
        self.dot.pack(side="right", padx=(0, 4))

        # --- 模式
        mode_row = ctk.CTkFrame(self, fg_color="transparent")
        mode_row.grid(row=1, column=0, sticky="ew", pady=(12, 0), **pad)
        ctk.CTkLabel(mode_row, text="重复", font=F_SMALL, text_color=C["sub"],
                     width=52, anchor="w").pack(side="left")
        self.mode_var = tk.StringVar(value="一次性")
        self.seg = ctk.CTkSegmentedButton(
            mode_row, values=["一次性", "每天", "工作日"], variable=self.mode_var,
            font=F_SMALL, height=30, corner_radius=8,
            fg_color=C["entry"], unselected_color=C["entry"],
            unselected_hover_color=C["entry_hover"],
            selected_color=accent_dim, selected_hover_color=accent_dim,
            text_color=C["sub"])
        self.seg.pack(side="left", fill="x", expand=True)

        # --- 时间
        time_row = ctk.CTkFrame(self, fg_color="transparent")
        time_row.grid(row=2, column=0, sticky="ew", pady=(12, 0), **pad)
        ctk.CTkLabel(time_row, text="时间", font=F_SMALL, text_color=C["sub"],
                     width=52, anchor="w").pack(side="left")
        self.hour = TimeStepper(time_row, maximum=23, initial=23)
        self.hour.pack(side="left")
        ctk.CTkLabel(time_row, text=":", font=F, text_color=C["sub"]).pack(side="left", padx=3)
        self.minute = TimeStepper(time_row, maximum=59, initial=30)
        self.minute.pack(side="left")
        self.time_row = time_row  # 附加控件（如“执行”菜单）直接排在此行

        # --- 附加选项行（子类填充）
        self.extra_row = ctk.CTkFrame(self, fg_color="transparent")
        self.extra_row.grid(row=3, column=0, sticky="ew", pady=(10, 0), **pad)

        # --- 按钮行
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=5, column=0, sticky="ew", pady=(12, 0), **pad)
        self.set_btn = ctk.CTkButton(
            btn_row, text="设置定时", height=40, corner_radius=10, font=F_BTN,
            fg_color=accent, hover_color=accent_hover, text_color=accent_text,
            command=self.on_set)
        self.set_btn.pack(side="left", fill="x", expand=True)
        self.cancel_btn = ctk.CTkButton(
            btn_row, text="取消定时", height=40, corner_radius=10, font=F_BTN,
            fg_color="transparent", border_width=1, border_color=C["border"],
            hover_color=C["entry_hover"], text_color=C["sub"],
            command=self.on_cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(10, 0), ipadx=6)

        # --- 状态行
        status_row = ctk.CTkFrame(self, fg_color="transparent")
        status_row.grid(row=6, column=0, sticky="ew", pady=(12, 18), **pad)
        self.detail = ctk.CTkLabel(status_row, text="当前未设置定时任务", font=F_SMALL,
                                   text_color=C["sub"], anchor="w", justify="left",
                                   wraplength=380)
        self.detail.pack(anchor="w")
        self.countdown = ctk.CTkLabel(status_row, text="", font=F_MONO,
                                      text_color=accent, anchor="w")
        self.countdown.pack(anchor="w", pady=(2, 0))

    # --- 由外部注入的回调
    def on_set(self):
        if self.callback_set:
            self.callback_set(self)

    def on_cancel(self):
        if self.callback_cancel:
            self.callback_cancel(self)

    callback_set = None
    callback_cancel = None

    # --- 状态刷新
    def update_status(self, info: dict | None, now: dt.datetime,
                      delay_next: dt.datetime | None = None):
        self.info = info
        if info is None:
            self.dot.configure(text_color=C["faint"])
            self.head_sub.configure(text=self.placeholder)
            self.detail.configure(text="当前未设置定时任务")
            self.countdown.configure(text="")
            self.cancel_btn.configure(state="disabled")
            return
        desc = self.describe(info)
        if not info["enabled"]:
            self.dot.configure(text_color=C["amber"])
            self.head_sub.configure(text="已暂停")
            if delay_next:
                self.detail.configure(
                    text=desc + f"（已延迟，将于 {humanize_time(delay_next, now)} 自动恢复并执行）")
            else:
                self.detail.configure(text=desc + "（任务已暂停）")
            self.countdown.configure(text="")
            self.cancel_btn.configure(state="normal" if not self.busy else "disabled")
            return
        nxt = next_run_of(info, now)
        if nxt:
            self.dot.configure(text_color=C["green"])
            self.head_sub.configure(text="已启用")
            self.detail.configure(text=desc)
            self.countdown.configure(
                text=f"下次执行 {humanize_time(nxt, now)} · 剩余 {fmt_delta(nxt - now)}")
        else:
            self.dot.configure(text_color=C["amber"])
            self.head_sub.configure(text="已过期")
            self.detail.configure(text=desc + "（任务已过期，等待自动清理或重新设置）")
            self.countdown.configure(text="")
        self.cancel_btn.configure(state="normal" if not self.busy else "disabled")

    def describe(self, info: dict) -> str:
        return describe_schedule(info)

    def get_values(self):
        return self.mode_var.get(), int(self.hour.get()), int(self.minute.get())

    def set_values(self, mode: str, hh: int, mm: int):
        self.mode_var.set(mode)
        self.hour.set(f"{hh:02d}")
        self.minute.set(f"{mm:02d}")

    def set_busy(self, busy: bool):
        self.busy = busy
        self.set_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="disabled" if busy else
                                  ("normal" if self.info else "disabled"))


# ---------------------------------------------------------------- 主窗口


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.title(APP_NAME)
        self.geometry("980x700")
        self.minsize(920, 640)
        self.configure(fg_color=C["bg"])
        # 主卡片行吸收多余空间；日志/贴士行保持固定高度
        self.grid_rowconfigure(1, weight=1)
        ico = resource_path("app.ico")
        if os.path.exists(ico):
            try:
                self.iconbitmap(ico)
            except Exception:
                pass

        self.admin = is_admin()
        self._hib_ok = True
        self._prefs = self._load_prefs()
        try:
            self._event_offset = os.path.getsize(EVENT_PATH)
        except OSError:
            self._event_offset = 0

        self._build_header()
        self._build_cards()
        self._build_bottom()
        self._build_statusbar()

        self.after(300, self._startup)
        self._tick()

    # ---------- 布局
    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=26, pady=(22, 10))
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(head, fg_color="transparent")
        left.pack(side="left")
        ctk.CTkLabel(left, text=APP_NAME, font=F_TITLE, text_color=C["text"],
                     anchor="w").pack(anchor="w")
        ctk.CTkLabel(left, text="Shutdown & Wake Scheduler · 基于 Windows 任务计划程序",
                     font=F_SUB, text_color=C["faint"], anchor="w").pack(anchor="w")

        right = ctk.CTkFrame(head, fg_color="transparent")
        right.pack(side="right")
        self.clock = ctk.CTkLabel(right, text="--:--:--", font=F_CLOCK,
                                  text_color=C["text"], anchor="e")
        self.clock.pack(anchor="e")
        self.date_lbl = ctk.CTkLabel(right, text="", font=F_SMALL,
                                     text_color=C["sub"], anchor="e")
        self.date_lbl.pack(anchor="e")
        badge_txt = "🛡 管理员模式" if self.admin else "普通模式"
        badge_col = C["green_dim"] if self.admin else C["gray_dim"]
        badge_fg = C["green"] if self.admin else C["faint"]
        ctk.CTkLabel(right, text=badge_txt, font=F_TINY, text_color=badge_fg,
                     fg_color=badge_col, corner_radius=8, height=20,
                     anchor="e").pack(anchor="e", pady=(6, 0), ipadx=8)

    def _build_cards(self):
        self.card_shutdown = PowerCard(
            self, title="定时关机", icon="🌙", accent=C["red"], accent_hover=C["red_hover"],
            accent_dim=C["red_dim"], accent_text=C["red_text"],
            placeholder="到点自动关机或休眠")
        self.card_shutdown.grid(row=1, column=0, sticky="nsew", padx=(26, 8), pady=6)

        self.card_wake = PowerCard(
            self, title="定时开机", icon="⏰", accent=C["amber"], accent_hover=C["amber_hover"],
            accent_dim=C["amber_dim"], accent_text=C["amber_text"],
            placeholder="从睡眠 / 休眠中唤醒电脑")
        self.card_wake.grid(row=1, column=1, sticky="nsew", padx=(8, 26), pady=6)

        self._build_shutdown_extras()
        self._build_wake_extras()

        self.card_shutdown.callback_set = self._set_shutdown
        self.card_shutdown.callback_cancel = self._cancel_shutdown
        self.card_wake.callback_set = self._set_wake
        self.card_wake.callback_cancel = self._cancel_wake

        p = self._prefs
        self.card_shutdown.set_values(p["shutdown"]["mode"], p["shutdown"]["h"], p["shutdown"]["m"])
        self.act_var.set(p["shutdown"].get("action", "关机"))
        self.force_var.set(p["shutdown"].get("force", False))
        self.card_wake.set_values(p["wake"]["mode"], p["wake"]["h"], p["wake"]["m"])
        self._sync_extra_row()

    def _build_shutdown_extras(self):
        ex = self.card_shutdown.time_row
        ctk.CTkLabel(ex, text="执行", font=F_SMALL, text_color=C["sub"]).pack(side="left", padx=(16, 4))
        self.act_var = tk.StringVar(value="关机")
        self.act_menu = ctk.CTkOptionMenu(
            ex, values=["关机", "休眠"], variable=self.act_var, width=92, height=34,
            font=F, dropdown_font=F, fg_color=C["entry"], button_color=C["entry_hover"],
            button_hover_color=C["entry_hover"], text_color=C["text"],
            dropdown_fg_color=C["entry"], dropdown_hover_color=C["entry_hover"],
            dropdown_text_color=C["text"], corner_radius=9,
            command=lambda _v: self._sync_extra_row())
        self.act_menu.pack(side="left")

        self.force_var = tk.BooleanVar(value=False)
        self.force_cb = ctk.CTkCheckBox(
            self.card_shutdown.extra_row, text="强制关机（不保存文件，直接断电）",
            variable=self.force_var, font=F_SMALL, text_color=C["sub"],
            fg_color=C["red"], hover_color=C["red_hover"], border_color=C["border"],
            checkbox_width=18, checkbox_height=18, corner_radius=5)
        self.force_cb.pack(side="left")

        self.hib_hint = ctk.CTkLabel(self.card_shutdown.extra_row,
                                     text="", font=F_TINY, text_color=C["amber"])
        self.hib_hint.pack(side="left", padx=(10, 0))

    def _build_wake_extras(self):
        ctk.CTkLabel(self.card_wake.extra_row,
                     text="唤醒方式：睡眠 / 休眠状态下有效（完全关机需 BIOS RTC 支持）",
                     font=F_TINY, text_color=C["faint"], anchor="w",
                     justify="left", wraplength=360).pack(anchor="w")

    def _sync_extra_row(self):
        if self.act_var.get() == "休眠":
            self.force_cb.pack_forget()
        else:
            self.force_cb.pack(side="left")

    def _build_bottom(self):
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=2, column=0, columnspan=2, sticky="ew", padx=26, pady=(8, 0))
        wrap.grid_columnconfigure(0, weight=3)
        wrap.grid_columnconfigure(1, weight=2)
        wrap.grid_rowconfigure(0, weight=1)

        log_card = ctk.CTkFrame(wrap, fg_color=C["card"], corner_radius=18,
                                border_width=1, border_color=C["border"])
        log_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=6)
        ctk.CTkLabel(log_card, text="操作日志", font=("Microsoft YaHei UI", 13, "bold"),
                     text_color=C["text"], anchor="w").pack(anchor="w", padx=18, pady=(12, 2))
        self.log_box = ctk.CTkTextbox(log_card, font=("Microsoft YaHei UI", 11),
                                      text_color=C["sub"], fg_color=C["entry"],
                                      corner_radius=10, wrap="word", height=120)
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(4, 14))
        self.log_box.configure(state="disabled")

        tips_card = ctk.CTkFrame(wrap, fg_color=C["card"], corner_radius=18,
                                 border_width=1, border_color=C["border"])
        tips_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=6)
        ctk.CTkLabel(tips_card, text="小贴士", font=("Microsoft YaHei UI", 13, "bold"),
                     text_color=C["text"], anchor="w").pack(anchor="w", padx=18, pady=(12, 2))
        ctk.CTkLabel(tips_card, justify="left", anchor="nw", font=F_SMALL,
                     text_color=C["sub"], wraplength=330,
                     text=(
                         "• 「定时开机」适用于 睡眠/休眠 状态；若电脑完全关机，"
                         "需在主板 BIOS 中开启 RTC 唤醒\n"
                         "• 关机执行前 1 分钟会弹窗提醒，可选择延迟 15 分钟或取消\n"
                         "• 唤醒无效时，检查 控制面板 → 电源选项 → 睡眠 → "
                         "「允许唤醒定时器」是否已启用\n"
                         "• 任务由 Windows 计划任务执行，关闭本程序后依然有效"
                     )).pack(anchor="w", padx=18, pady=(2, 14))

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=26, pady=(2, 10))
        ctk.CTkLabel(bar, text=f"v{APP_VERSION}", font=F_TINY,
                     text_color=C["faint"]).pack(side="left")
        ctk.CTkLabel(bar, text="计划任务前缀 PowerScheduler_*（关机 / 提醒 / 延迟 / 唤醒）",
                     font=F_TINY, text_color=C["faint"]).pack(side="right")

    # ---------- 时钟
    def _tick(self):
        now = dt.datetime.now()
        self.clock.configure(text=now.strftime("%H:%M:%S"))
        self.date_lbl.configure(
            text=f"{now.year}年{now.month}月{now.day}日 · {WEEKDAY_CN[now.weekday()]}")
        for card in (self.card_shutdown, self.card_wake):
            if card.info:
                card.update_status(card.info, now)
        self._poll_warn_events()
        self.after(1000, self._tick)

    def _poll_warn_events(self):
        try:
            with open(EVENT_PATH, "rb") as f:
                f.seek(self._event_offset)
                data = f.read()
        except OSError:
            return
        consumed = 0
        received = False
        for line in data.splitlines(keepends=True):
            if not line.endswith(b"\n"):
                break
            consumed += len(line)
            try:
                event = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            action = event.get("action")
            verb = event.get("verb", "关机")
            if action == "delay":
                msg = (f"✓ 提醒窗口已将定时{verb}延迟 {DELAY_MINUTES} 分钟：{event['message']}"
                       if event.get("success") else f"✗ 提醒窗口延迟失败：{event.get('message', '')}")
            elif action == "cancel":
                msg = (f"✓ 已从提醒窗口取消本次自动{verb}"
                       if event.get("success") else f"✗ 提醒窗口取消失败：{event.get('message', '')}")
            else:
                continue
            self.log(msg)
            received = True
        self._event_offset += consumed
        if received:
            self._refresh_status()

    # ---------- 启动流程
    def _startup(self):
        self.log(f"{APP_NAME} v{APP_VERSION} 已启动（{'管理员' if self.admin else '普通'}模式）")
        self._async(hibernate_available, self._on_hib_check)
        self._async(startup_maintenance, self._on_maint)

    def _on_maint(self, msgs):
        for m in msgs:
            self.log(m)
        self._refresh_status(initial=True)

    def _on_hib_check(self, ok):
        self._hib_ok = bool(ok)
        if ok:
            self.log("系统休眠功能：可用")
            self.act_menu.configure(values=["关机", "休眠"])
        else:
            self.log("系统休眠功能：未启用（如需休眠定时，可先开启休眠）")
            self.act_menu.configure(values=["关机"])
            if self.act_var.get() == "休眠":
                self.act_var.set("关机")
            self._sync_extra_row()
            if self.admin:
                self.hib_hint.configure(text="休眠未开启，点击此处一键开启 ▸")
                self.hib_hint.bind("<Button-1>", lambda e: self._enable_hibernate())

    def _enable_hibernate(self):
        self._async(enable_hibernate, self._on_hib_enabled)
        self.hib_hint.configure(text="正在开启休眠…")

    def _on_hib_enabled(self, res):
        ok, msg = res
        if ok:
            self.log("休眠功能已开启")
            self.hib_hint.configure(text="✓ 休眠已开启", text_color=C["green"])
            self.act_menu.configure(values=["关机", "休眠"])
        else:
            self.log(f"开启休眠失败：{msg}")
            self.hib_hint.configure(text="开启失败，请以管理员运行 powercfg /h on",
                                    text_color=C["red"])

    # ---------- 日志
    def log(self, msg):
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ---------- 异步工具
    def _async(self, fn, cb):
        def worker():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                res = ("__EXC__", str(e))
            self.after(0, lambda: cb(res))
        threading.Thread(target=worker, daemon=True).start()

    def _unwrap(self, res):
        if isinstance(res, tuple) and len(res) == 2 and res[0] == "__EXC__":
            raise RuntimeError(res[1])
        return res

    # ---------- 状态刷新
    def _refresh_status(self, initial=False):
        def do_query():
            delay_info = query_task(TASK_SHUTDOWN_DELAY)
            return (query_task(TASK_SHUTDOWN), query_task(TASK_WAKE),
                    next_run_of(delay_info, dt.datetime.now()))

        def done(res):
            try:
                sd, wk, delay_next = self._unwrap(res)
            except Exception as e:  # noqa: BLE001
                self.log(f"查询任务状态失败：{e}")
                return
            now = dt.datetime.now()
            self.card_shutdown.update_status(sd, now, delay_next)
            self.card_wake.update_status(wk, now)
            if initial:
                if sd:
                    self.log(f"检测到已有定时关机任务：{self.card_shutdown.describe(sd)}")
                if wk:
                    self.log(f"检测到已有定时唤醒任务：{self.card_wake.describe(wk)}")
                if not sd and not wk:
                    self.log("当前没有任何定时任务")
        self._async(do_query, done)

    # ---------- 定时关机
    def _set_shutdown(self, card: PowerCard):
        mode, h, m = card.get_values()
        action = self.act_var.get()
        force = self.force_var.get() if action == "关机" else False
        if action == "休眠" and not self._hib_ok:
            ConfirmDialog(self, "无法设置", "系统未开启休眠功能。\n可先运行：powercfg /h on",
                          show_cancel=False, danger=True)
            return
        now = dt.datetime.now()
        if mode == "一次性":
            target = next_occurrence({"一次性": "once", "每天": "daily",
                                      "工作日": "workday"}[mode], h, m, now)
            when = humanize_time(target, now)
        else:
            when = {"每天": f"每天 {h:02d}:{m:02d}", "工作日": f"每个工作日 {h:02d}:{m:02d}"}[mode]
        verb = "关机" if action == "关机" else "休眠"
        extra = "（强制，不保存文件）" if force else ""
        mode_key = {"一次性": "once", "每天": "daily", "工作日": "workday"}[mode]

        def do_set():
            return create_shutdown_task(mode_key, h, m, force, action)

        def confirmed():
            card.set_busy(True)
            self.log(f"正在设置定时{verb}：{when}{extra} …")

            def done(res):
                card.set_busy(False)
                try:
                    ok, err, warn_ok = self._unwrap(res)
                except Exception as e:  # noqa: BLE001
                    ok, err, warn_ok = False, str(e), False
                if ok:
                    if warn_ok:
                        self.log(f"✓ 定时{verb}已设置：{when}{extra}（执行前 1 分钟弹窗提醒）")
                    else:
                        self.log(f"✓ 定时{verb}已设置：{when}{extra}（临近执行，未设提醒弹窗）")
                    self._save_prefs()
                    self._refresh_status()
                else:
                    self.log(f"✗ 设置失败：{err}")
                    ConfirmDialog(self, "设置失败", f"创建计划任务失败：\n{err}",
                                  show_cancel=False, danger=True)
            self._async(do_set, done)

        ConfirmDialog(
            self, f"确认定时{verb}",
            f"将在 {when} 自动{verb}{extra}。\n\n"
            f"执行前 1 分钟会弹窗提醒，可选择「延迟 15 分钟」或「取消关机」。\n\n确定设置吗？",
            confirm_text="确认设置", danger=(action == "关机"),
            on_confirm=confirmed)

    def _cancel_shutdown(self, card: PowerCard):
        def do_cancel():
            delete_shutdown_tasks()
            return True

        card.set_busy(True)

        def done(res):
            card.set_busy(False)
            self._unwrap(res)
            self.log("✓ 已取消定时关机（含提醒与延迟任务）")
            self._refresh_status()
        self._async(do_cancel, done)

    # ---------- 定时唤醒
    def _set_wake(self, card: PowerCard):
        mode, h, m = card.get_values()
        mode_key = {"一次性": "once", "每天": "daily", "工作日": "workday"}[mode]
        now = dt.datetime.now()
        target = next_occurrence(mode_key, h, m, now)
        if mode == "一次性":
            when = humanize_time(target, now)
        else:
            when = {"每天": f"每天 {h:02d}:{m:02d}", "工作日": f"每个工作日 {h:02d}:{m:02d}"}[mode]
        dows = DOW_DAILY if mode_key == "workday" else None

        def do_set():
            return register_task(
                TASK_WAKE, mode_key, target, dows,
                command="cmd.exe", arguments="/c exit", wake=True,
                description=f"{APP_NAME} 定时唤醒（从睡眠/休眠唤醒）")

        def confirmed():
            card.set_busy(True)
            self.log(f"正在设置定时唤醒：{when} …")

            def done(res):
                card.set_busy(False)
                try:
                    ok, err = self._unwrap(res)
                except Exception as e:  # noqa: BLE001
                    ok, err = False, str(e)
                if ok:
                    self.log(f"✓ 定时唤醒已设置：{when}（睡眠/休眠状态下自动唤醒）")
                    self._save_prefs()
                    self._refresh_status()
                else:
                    self.log(f"✗ 设置失败：{err}")
                    ConfirmDialog(self, "设置失败",
                                  f"创建计划任务失败：\n{err}\n\n"
                                  "提示：唤醒任务可能需要以管理员身份运行本程序。",
                                  show_cancel=False, danger=True)
            self._async(do_set, done)

        ConfirmDialog(self, "确认定时唤醒",
                      f"将在 {when} 唤醒电脑（睡眠/休眠状态下有效）。\n\n确定设置吗？",
                      confirm_text="确认设置", danger=False, on_confirm=confirmed)

    def _cancel_wake(self, card: PowerCard):
        def do_cancel():
            return delete_task(TASK_WAKE)

        card.set_busy(True)

        def done(res):
            card.set_busy(False)
            ok = self._unwrap(res)
            if ok:
                self.log("✓ 已取消定时唤醒")
            else:
                self.log("✗ 取消失败（任务可能已不存在）")
            self._refresh_status()
        self._async(do_cancel, done)

    # ---------- 偏好
    def _load_prefs(self):
        default = {
            "shutdown": {"mode": "一次性", "h": 23, "m": 30, "action": "关机", "force": False},
            "wake": {"mode": "一次性", "h": 8, "m": 0},
        }
        try:
            with open(PREFS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in default:
                if k in data:
                    default[k].update(data[k])
        except Exception:
            pass
        return default

    def _save_prefs(self):
        p = {
            "shutdown": {
                "mode": self.card_shutdown.mode_var.get(),
                "h": int(self.card_shutdown.hour.get()),
                "m": int(self.card_shutdown.minute.get()),
                "action": self.act_var.get(),
                "force": bool(self.force_var.get()),
            },
            "wake": {
                "mode": self.card_wake.mode_var.get(),
                "h": int(self.card_wake.hour.get()),
                "m": int(self.card_wake.minute.get()),
            },
        }
        try:
            with open(PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


# ---------------------------------------------------------------- 关机提醒弹窗


class WarnWindow(ctk.CTk):
    """关机执行前 1 分钟的提醒弹窗（由计划任务以 --warn 参数拉起）。
    「知道了」= 关闭弹窗照常执行；「延迟」= 推迟 15 分钟；「取消关机」= 删除任务。"""

    def __init__(self, info: dict):
        super().__init__()
        self.info = info
        self.args = info.get("arguments") or "/s /t 0"
        self.verb = "休眠" if self.args.strip().startswith("/h") else "关机"
        now = dt.datetime.now()
        nxt = next_run_of(info, now)
        if nxt is None:
            self.after(10, self.destroy)
            return
        self.remaining = max(5, min(120, int((nxt - now).total_seconds())))
        self.closed = False

        self.title("定时关机提醒")
        self.geometry("560x420")
        self.resizable(False, False)
        self.configure(fg_color=C["bg"])
        self.attributes("-topmost", True)
        ico = resource_path("app.ico")
        if os.path.exists(ico):
            try:
                self.iconbitmap(ico)
            except Exception:
                pass
        self.protocol("WM_DELETE_WINDOW", self._ok)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self._ok())

        card = ctk.CTkFrame(self, fg_color=C["card"], corner_radius=18,
                            border_width=1, border_color=C["border"])
        card.pack(fill="both", expand=True, padx=20, pady=20)
        bar = ctk.CTkFrame(card, fg_color=C["red"], corner_radius=3, height=4)
        bar.pack(fill="x", padx=24, pady=(22, 0))

        icon = ctk.CTkLabel(card, text="🌙", width=64, height=64,
                            font=("Segoe UI Emoji", 30), fg_color=C["red_dim"],
                            corner_radius=20, text_color=C["red"])
        icon.pack(pady=(22, 8))

        ctk.CTkLabel(card, text=f"电脑即将自动{self.verb}", font=F_CARD,
                     text_color=C["text"]).pack()
        ctk.CTkLabel(card, text=f"任务：{describe_schedule(info)} · 执行前 1 分钟提醒",
                     font=F_SMALL, text_color=C["sub"]).pack(pady=(4, 10))

        self.cd_lbl = ctk.CTkLabel(card, text="--:--", font=F_HUGE, text_color=C["red"])
        self.cd_lbl.pack(pady=(2, 2))
        ctk.CTkLabel(card, text="倒计时结束后将立即执行", font=F_TINY,
                     text_color=C["faint"]).pack()

        btns = ctk.CTkFrame(card, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(20, 0))
        self.btn_ok = ctk.CTkButton(
            btns, text="知道了", width=110, height=42, font=F_BTN,
            fg_color=C["entry"], hover_color=C["entry_hover"],
            text_color=C["sub"], corner_radius=10, command=self._ok)
        self.btn_ok.pack(side="left", expand=True, fill="x")
        self.btn_delay = ctk.CTkButton(
            btns, text=f"延迟 {DELAY_MINUTES} 分钟", width=140, height=42,
            font=F_BTN, fg_color=C["amber"], hover_color=C["amber_hover"],
            text_color=C["amber_text"], corner_radius=10, command=self._delay)
        self.btn_delay.pack(side="left", padx=10, expand=True, fill="x")
        self.btn_cancel = ctk.CTkButton(
            btns, text="取消关机", width=130, height=42, font=F_BTN,
            fg_color=C["red"], hover_color=C["red_hover"],
            text_color=C["red_text"], corner_radius=10, command=self._cancel)
        self.btn_cancel.pack(side="left", expand=True, fill="x")

        self.feedback = ctk.CTkLabel(card, text="", font=F_SMALL, text_color=C["sub"],
                                     justify="left", wraplength=480)
        self.feedback.pack(pady=(14, 0))
        ctk.CTkLabel(card, text="直接关闭本窗口或按回车 = 照常执行",
                     font=F_TINY, text_color=C["faint"]).pack(pady=(4, 18))

        self.after(50, self._show)
        self._tick()

    def _show(self):
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2 - 60}")
        self.deiconify()
        self.lift()
        self.attributes("-topmost", True)
        self.focus_force()

    def _tick(self):
        if self.closed:
            return
        if self.remaining <= 0:
            self.destroy()  # 到点：由计划任务执行关机
            return
        m, s = divmod(self.remaining, 60)
        self.cd_lbl.configure(text=f"{m:02d}:{s:02d}")
        self.remaining -= 1
        self.after(1000, self._tick)

    def _set_buttons(self, state):
        for b in (self.btn_ok, self.btn_delay, self.btn_cancel):
            try:
                b.configure(state=state)
            except Exception:
                pass

    def _ok(self):
        self.closed = True
        self.destroy()

    def _delay(self):
        self._set_buttons("disabled")
        self.feedback.configure(text="正在延迟…", text_color=C["amber"])
        threading.Thread(target=self._do_delay, daemon=True).start()

    def _do_delay(self):
        try:
            ok, msg = delay_shutdown(DELAY_MINUTES)
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        record_warn_event("delay", ok, msg, self.verb)

        def apply():
            if self.closed:
                return
            if ok:
                self.feedback.configure(
                    text=f"✓ 已延迟 {DELAY_MINUTES} 分钟，将于 {msg} 自动{self.verb}",
                    text_color=C["green"])
                self.after(1600, self._ok)
            else:
                self.feedback.configure(text=f"✗ 延迟失败：{msg}", text_color=C["red"])
                self._set_buttons("normal")
        self.after(0, apply)

    def _cancel(self):
        self._set_buttons("disabled")
        self.feedback.configure(text="正在取消…", text_color=C["amber"])
        threading.Thread(target=self._do_cancel, daemon=True).start()

    def _do_cancel(self):
        msg = ""
        try:
            delete_shutdown_tasks()
            ok = query_task(TASK_SHUTDOWN) is None
        except Exception as e:  # noqa: BLE001
            ok, msg = False, str(e)
        record_warn_event("cancel", ok, msg, self.verb)

        def apply():
            if self.closed:
                return
            if ok:
                self.feedback.configure(
                    text=f"✓ 已取消本次自动{self.verb}，任务已删除", text_color=C["green"])
                self.after(1600, self._ok)
            else:
                self.feedback.configure(text=f"✗ 取消失败：{msg or '请打开主程序处理'}",
                                        text_color=C["red"])
                self._set_buttons("normal")
        self.after(0, apply)


def main():
    if "--warn" in sys.argv[1:]:
        info = query_task(TASK_SHUTDOWN)
        if not info or next_run_of(info, dt.datetime.now()) is None:
            return  # 任务已被取消/不存在，无需提醒
        WarnWindow(info).mainloop()
        return
    App().mainloop()


if __name__ == "__main__":
    main()
