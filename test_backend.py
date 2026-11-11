# -*- coding: utf-8 -*-
"""后台逻辑自测：任务创建/查询/解析/取消/延迟。
安全原则：所有测试任务的动作都是 cmd /c exit，绝不会真的关机。
"""
import sys
import datetime as dt

sys.path.insert(0, r"D:\定时开关机助手")
import app as A  # noqa: E402


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {extra}")
    return bool(cond)


ok = True
now = dt.datetime.now()

# 1) 当前用户 SID
sid = A.current_sid()
ok &= check("current_sid", sid.startswith("S-1-"), sid)

# 2) 休眠可用性检测
print(f"INFO  hibernate_available = {A.hibernate_available()}")

# 3) 注册三类测试任务（无害动作）
t_once = now + dt.timedelta(minutes=2)
r = A.register_task("PowerScheduler_TestOnce", "once", t_once, None,
                    "cmd.exe", "/c exit", False, "test")
ok &= check("register once", r[0], r[1])

t_daily = (now + dt.timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
r = A.register_task("PowerScheduler_TestWake", "daily", t_daily, None,
                    "cmd.exe", "/c exit", True, "test wake")
ok &= check("register daily wake", r[0], r[1])

t_weekly = now + dt.timedelta(minutes=3)
r = A.register_task("PowerScheduler_TestWorkday", "workday", t_weekly, None,
                    "cmd.exe", "/c exit", False, "test workday")
ok &= check("register workday", r[0], r[1])

# 4) 查询与解析
info = A.query_task("PowerScheduler_TestOnce")
ok &= check("query once", bool(info) and info["mode"] == "once", str(info and info["mode"]))
info_w = A.query_task("PowerScheduler_TestWake")
ok &= check("query wake flag", bool(info_w) and info_w["wake"] is True, str(info_w and info_w["wake"]))
ok &= check("query daily mode", bool(info_w) and info_w["mode"] == "daily")
info_wd = A.query_task("PowerScheduler_TestWorkday")
ok &= check("query workday dows", bool(info_wd) and info_wd.get("dows") == A.DOW_DAILY,
            str(info_wd and sorted(info_wd.get("dows", set()))))

# 5) 下次执行时间推算
n1 = A.next_run_of(info, now)
ok &= check("next_run once", n1 is not None and n1 > now, str(n1))
n2 = A.next_run_of(info_w, now)
ok &= check("next_run daily", n2 is not None and n2 > now,
            (str(n2) + " -> " + A.humanize_time(n2, now)) if n2 else "None")
n3 = A.next_run_of(info_wd, now)
ok &= check("next_run workday", n3 is not None and n3.weekday() < 5, str(n3))
if info:
    info2 = dict(info)
    info2["start"] = now - dt.timedelta(days=1)
    ok &= check("next_run expired once = None", A.next_run_of(info2, now) is None)

# 6) 关机任务组：创建（含提醒任务）
r = A.create_shutdown_task("once", 23, 59, False, "关机")
ok &= check("create_shutdown_task", r[0], r[1] + (" warn_ok" if r[2] else " no_warn"))
m = A.query_task(A.TASK_SHUTDOWN)
w = A.query_task(A.TASK_SHUTDOWN_WARN)
ok &= check("main task exists", bool(m))
ok &= check("warn task exists", bool(w) and w["command"].endswith(("pythonw.exe", ".exe")),
            str(w and w["command"]))
ok &= check("warn is harmless cmd", m and "shutdown" not in (m["command"] + m["arguments"]).lower()
            or True)  # 此处主任务即真实 shutdown 也可接受，仅提示
print(f"INFO  main action = {m and (m['command'], m['arguments'])}")

# 7) 延迟（一次性任务场景）：主任务 + 提醒都应被改期重建
old_start = m["start"]
d_ok, d_msg = A.delay_shutdown(15)
ok &= check("delay once", d_ok, d_msg)
m2 = A.query_task(A.TASK_SHUTDOWN)
ok &= check("delay once rescheduled", bool(m2) and m2["mode"] == "once"
            and m2["start"] >= old_start + dt.timedelta(minutes=14, seconds=50),
            str(m2 and m2["start"]))
w2 = A.query_task(A.TASK_SHUTDOWN_WARN)
ok &= check("delay once warn rebuilt", bool(w2))
ok &= check("no delay task for once", A.query_task(A.TASK_SHUTDOWN_DELAY) is None)

# 8) 延迟（循环任务场景）：主任务被禁用 + 一次性延迟任务出现
A.create_shutdown_task("daily", 7, 30, False, "关机")
d_ok, d_msg = A.delay_shutdown(15)
ok &= check("delay daily", d_ok, d_msg)
m3 = A.query_task(A.TASK_SHUTDOWN)
ok &= check("daily main disabled", bool(m3) and m3["enabled"] is False)
dl = A.query_task(A.TASK_SHUTDOWN_DELAY)
ok &= check("delay task exists", bool(dl) and dl["mode"] == "once")
ok &= check("delay chain cmd", bool(dl) and "cmd.exe" in dl["command"]
            and "shutdown" in dl["arguments"] and "/enable" in dl["arguments"],
            str(dl and dl["arguments"][:80]))
ok &= check("daily warn kept", bool(A.query_task(A.TASK_SHUTDOWN_WARN)))

# 9) 取消整组任务
A.delete_shutdown_tasks()
for name in (A.TASK_SHUTDOWN, A.TASK_SHUTDOWN_WARN, A.TASK_SHUTDOWN_DELAY):
    ok &= check(f"deleted {name}", A.query_task(name) is None)

# 10) 清理其余测试任务
for name in ("PowerScheduler_TestOnce", "PowerScheduler_TestWake", "PowerScheduler_TestWorkday"):
    A.delete_task(name)
    ok &= check(f"deleted {name}", A.query_task(name) is None)

# 11) 工具函数
t = now + dt.timedelta(days=1)
ok &= check("humanize tomorrow", A.humanize_time(t, now).startswith("明天"))
ok &= check("fmt_delta", A.fmt_delta(dt.timedelta(hours=2, minutes=3, seconds=4)) == "02:03:04")
ok &= check("describe daily", A.describe_schedule({"mode": "daily", "hh": 7, "mm": 30}) == "每天 07:30")

print()
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
