#!/usr/bin/python3
"""Optional GTK3 desktop input receiver. It does not inject input or change settings."""
import argparse
from collections import Counter
import json
import os
import signal
import time


def _device_name(event, method):
    try:
        device = getattr(event, method)()
        return device.get_name() if device else None
    except (AttributeError, TypeError):
        return None


def parse_event(event, gdk):
    """Own the Gdk.Event union: expose only accessor-decoded facts to consumers."""
    event_type = event.get_event_type()
    if event_type == gdk.EventType.KEY_PRESS:
        available, keyval = event.get_keyval()
        return {"kind": "escape"} if available and keyval == gdk.KEY_Escape else None
    kind = {gdk.EventType.TOUCH_BEGIN: "touch_begin", gdk.EventType.TOUCH_UPDATE: "touch_update",
            gdk.EventType.TOUCH_END: "touch_end", gdk.EventType.TOUCH_CANCEL: "touch_cancel"}.get(event_type)
    fields = {}
    if event_type in (gdk.EventType.BUTTON_PRESS, gdk.EventType.BUTTON_RELEASE):
        available, button = event.get_button()
        if not available or button not in (1, 2, 3):
            return None
        kind = "button_press" if event_type == gdk.EventType.BUTTON_PRESS else "button_release"
        fields["button"] = button
    elif event_type == gdk.EventType.SCROLL:
        deltas_available, dx, dy = event.get_scroll_deltas()
        available, direction = event.get_scroll_direction()
        if deltas_available:
            kind = "scroll"
            fields.update(direction="smooth", deltas_available=True, delta_x=dx, delta_y=dy)
        elif available:
            kind, fields["direction"] = "scroll", direction.value_nick
        else:
            return None
    if kind is None:
        return None
    available, x, y = event.get_coords()
    return {"kind": kind, "gdk_time_ms": event.get_time(), "coords_available": available,
            "x": x if available else None, "y": y if available else None,
            "pointer_emulated": bool(event.get_pointer_emulated()),
            "device_name": _device_name(event, "get_device"),
            "source_device_name": _device_name(event, "get_source_device"), **fields}


def count_event(counts, facts):
    kind, emulated = facts["kind"], facts["pointer_emulated"]
    if emulated:
        counts["pointer_emulated"] += 1
    if kind in ("button_press", "button_release"):
        if not emulated:
            counts[f"{kind}_{facts['button']}"] += 1
    elif kind == "scroll":
        if facts["direction"] == "smooth":
            if facts["deltas_available"] and not emulated:
                counts["smooth_scroll_events"] += 1
                counts["smooth_scroll_delta_x"] += facts["delta_x"]
                counts["smooth_scroll_delta_y"] += facts["delta_y"]
        elif not emulated and facts["direction"] in ("up", "down"):
            counts[f"scroll_{facts['direction']}"] += 1
    else:
        counts[kind] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New JSONL evidence file; never overwrite")
    parser.add_argument("--seconds", type=int, default=120, help="Automatic close after 1..120 seconds")
    parser.add_argument("--stage", choices=("all", "left", "right", "middle", "wheel", "touch"), default="all")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120:
        parser.error("--seconds must be 1..120")
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    if not Gtk.init_check()[0]:
        parser.error("GTK could not connect to the current desktop display")
    output_fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    counts, finished, ready = Counter(), False, False
    started = time.monotonic_ns()
    with os.fdopen(output_fd, "w", encoding="utf-8") as stream:
        def write(kind, **fields):
            row = {"kind": kind, "wall_time_ns": time.time_ns(),
                   "monotonic_ns": time.monotonic_ns(), **fields}
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            return row
        window = Gtk.Window(title="RGR06 桌面输入验证")
        window.set_default_size(900, 650)
        label = Gtk.Label()
        label.set_margin_start(32)
        label.set_margin_end(32)
        label.set_line_wrap(True)
        window.add(label)
        instructions = (
            "RGR06 桌面输入验证\n\n"
            "先轻触并松开 RGR06，等待映射启动约 2 秒。\n"
            "再左键、右键、滚轮中键各单击一次；向前、向后各转 3 格；触摸滑动。\n"
            "最后使用普通键盘 Esc 退出。窗口最多 120 秒后自动关闭。\n\n"
        )
        if args.stage != "all":
            action = {"left": "只持续单击左键。不要触摸、转动滚轮或按其他设备键。",
                      "right": "只持续单击右键。不要触摸、转动滚轮或按其他设备键。",
                      "middle": "只持续单击滚轮中键。不要触摸、转动滚轮或按其他设备键。",
                      "wheel": "只向前、向后各转动滚轮 3 格。不要触摸或点击设备键。",
                      "touch": "只触摸滑动，然后松开。不要转动滚轮或点击设备键。"}[args.stage]
            instructions = ("RGR06 桌面输入验证\n\n" + action +
                            "\n最后使用普通键盘 Esc 退出。窗口最多 120 秒后自动关闭。\n\n")
        def redraw():
            label.set_text(instructions +
                f"非触摸模拟按钮按下/抬起：左 {counts['button_press_1']}/{counts['button_release_1']}  "
                f"中 {counts['button_press_2']}/{counts['button_release_2']}  "
                f"右 {counts['button_press_3']}/{counts['button_release_3']}\n"
                f"滚轮：向前 {counts['scroll_up']}  向后 {counts['scroll_down']}\n"
                f"触摸：开始 {counts['touch_begin']}  移动 {counts['touch_update']}  "
                f"结束 {counts['touch_end']}  取消 {counts['touch_cancel']}\n"
                f"触摸模拟指针事件：{counts['pointer_emulated']}\n\n请在此窗口内操作。")
        def finish(reason):
            nonlocal finished
            if finished:
                return False
            finished = True
            summary = write("summary", reason=reason, elapsed_seconds=(time.monotonic_ns()-started)/1e9,
                            counts=dict(counts), pid=os.getpid(), stage=args.stage)
            os.fsync(stream.fileno())
            print(json.dumps(summary, ensure_ascii=False), flush=True)
            window.destroy()
            Gtk.main_quit()
            return False
        def receive(widget, event):
            facts = parse_event(event, Gdk)
            if facts is None:
                return False
            if facts["kind"] == "escape":
                finish("escape")
                return True
            count_event(counts, facts)
            write(facts["kind"], **{key: value for key, value in facts.items() if key != "kind"})
            redraw()
            return True  # Consume once, before GtkWindow's WM check can replay the event.
        def mapped(widget, event):
            nonlocal ready
            if not ready:
                ready = True
                write("ready", pid=os.getpid(), output_absolute_path=os.path.abspath(args.output),
                      timeout_seconds=args.seconds, gtk_version=Gtk.get_major_version(), stage=args.stage,
                      limits="Counts cover input delivered to this window; device names may be compositor-dependent.")
                print(f"READY pid={os.getpid()}", flush=True)
            return False
        window.add_events(Gdk.EventMask.TOUCH_MASK | Gdk.EventMask.BUTTON_PRESS_MASK |
                          Gdk.EventMask.BUTTON_RELEASE_MASK | Gdk.EventMask.SCROLL_MASK |
                          Gdk.EventMask.SMOOTH_SCROLL_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        window.connect("event", receive)
        window.connect("map-event", mapped)
        window.connect("delete-event", lambda *unused: finish("window_closed") or True)
        old_handlers = {}
        for number in (signal.SIGINT, signal.SIGTERM):
            old_handlers[number] = signal.signal(number, lambda number, frame: GLib.idle_add(finish, f"signal_{number}"))
        GLib.timeout_add(args.seconds * 1000, finish, "timeout")
        redraw()
        window.fullscreen()
        window.show_all()
        window.present()
        try:
            Gtk.main()
        finally:
            if not finished:
                finish("main_loop_ended")
            for number, handler in old_handlers.items():
                signal.signal(number, handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
