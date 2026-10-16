# 运行与自定义

安装和设备权限见 [install.md](install.md)，功能与验证范围见 [readme.md](readme.md)。以下命令均在当前电脑执行。

## 启动

```bash
cd /home/longxiaoze/codes/rgr06-tools
python3 rgr06_mapper.py --check
python3 rgr06_mapper.py --run --seconds 3600 --arm-timeout 120
```

`--check` 检查目标身份、描述符和访问权限，不抓取设备、不创建虚拟设备、不注入输入。设备空闲时 `live_report_length` 可以是 `null`，表示这次检查未收到实时报告。

执行 `--run` 后，按终端提示**轻触并松开触摸区，移开手指，放开所有按键**，等待出现 `Mapping active` 后再使用。握手阶段的报告不会重放为桌面操作。运行期间只临时接管这台 RGR06 的两个原生输入接口。

| 参数 | 默认值 | 有效范围 |
| --- | --- | --- |
| `--seconds` | 300 秒 | 有限数，`0 < seconds <= 3600`，从映射激活后计时 |
| `--arm-timeout` | 30 秒 | 有限数，`0 < arm-timeout <= 120`，等待首包和全部控件松开共用这个期限 |

同一目录的映射工具只能运行一个实例。关闭旧实例后再启动；不要通过删除锁文件绕过正在运行的实例。

按 **Ctrl-C** 停止。达到时限或蓝牙断连时也会退出，并尝试释放可能按下的虚拟键和设备占用。断连后重新连接、检查权限，再重新启动；工具不会自动重连启动。

## 修改映射

先停止映射并备份配置；以下备份名称带日期，同名文件不会被覆盖。确认备份已保存后再编辑。

```bash
backup_path="rgr06-config.backup-$(TZ=Asia/Tokyo date +%Y%m%d-%H%M%S).json"
cp --no-clobber -- rgr06-config.json "$backup_path"
```

当前默认配置：

```json
{
  "schema_version": 1,
  "profile": "rgr06-observed-v1",
  "device": {"uniq": "d1:05:cb:5c:37:15"},
  "touch": {
    "enabled": true,
    "mode": "pointer",
    "gain": 0.5,
    "contact_signal": "in_range",
    "swap_xy": false,
    "invert_x": false,
    "invert_y": false
  },
  "controls": {
    "left": "left",
    "right": "right",
    "wheel_press": "middle",
    "wheel_forward": "wheel_up",
    "wheel_backward": "wheel_down"
  }
}
```

`controls` 左侧分别表示物理左键、右键、滚轮按下、滚轮向前、滚轮向后；右侧可选动作只有 `left`、`right`、`middle`、`wheel_up`、`wheel_down`。例如把 `"right": "right"` 改成 `"right": "middle"`，物理右键就输出鼠标中键单击。当前配置不提供任意键盘快捷键或自定义宏执行。

`touch.mode="pointer"` 把滑动转换成鼠标光标的相对移动。每次接触先建立基线，抬手后重新放下不会直接跳转光标；触摸本身不生成单击，点击由实体按键完成。

- `gain` 默认 `0.5`，增大更快、减小更慢，必须是有限正数。
- `swap_xy` 交换两个方向；`invert_x`、`invert_y` 翻转对应方向。
- `enabled=false` 关闭触摸映射，实体按键和滚轮仍按配置工作。
- `contact_signal="in_range"` 来自本机实测：滑动报告使用 InRange，松手后清零；只依赖 TipSwitch 会漏掉这些滑动。使用这台设备时保留该设置。

备选 `touch.mode="touchscreen"` 创建系统单点触摸屏，坐标范围为 `0..4095`，直接映射屏幕位置；它不会像触控板模式一样移动鼠标箭头。`gain` 用于触控板模式。

保存配置后重启工具才会加载修改。也可以明确选择另一份配置：

```bash
python3 rgr06_mapper.py --check --config rgr06-config.json
python3 rgr06_mapper.py --run --config rgr06-config.json --seconds 3600 --arm-timeout 120
```

## 采样与回放

被动采样不会抓取设备或注入输入，但原生设备操作仍可能影响当前桌面。每次使用一个**尚不存在**的输出文件；`record` 不覆盖已有文件，时长必须是有限正数且不超过 600 秒。

```bash
python3 rgr06_capture.py inspect
python3 rgr06_capture.py record --seconds 60 --output evidence/my-new-capture.jsonl
python3 rgr06_mapper.py --replay evidence/retest-samples.jsonl
```

`--replay` 校验录音来源并在计数器中回放，不打开输入设备、不注入桌面输入。它使用当前选定配置，因此触控板模式和触摸屏模式的统计会不同。

完整的 `retest-samples.jsonl` 有 1468 条原始 HID 报告；当前解析得到向上 49、向下 51 个逻辑滚动单位。连续方向 tap 支持同段内反向，末尾 Up/Down 只结束该段，不额外滚动。物理每格与逻辑单位的比例尚未标定。该录音另有 6 个未识别序列，涉及尚未实现的多击等按钮宏；物理长按、按住拖拽及未采样宏不作支持保证。

硬件坐标可能在 0 或 4095 饱和，软件无法恢复已丢失的位移。可尝试反向滑动并检查原始坐标；抬手只会清除软件基线，不保证设备内部坐标复位。

## 排障与可选窗口测试

1. **权限不足**：运行 `python3 rgr06_capture.py inspect`，确认完整目标身份及本次实际节点路径，按 [install.md](install.md) 仅授权对应节点。重连后节点编号和 ACL 可能改变；不要沿用旧路径，也不要开放全部输入设备。输入节点读取和 `/dev/uinput` 写入都需要相应权限。
2. **没有首包或未完全松开**：在握手期限内轻触、松开并移开手指，实体按键也全部放开。超时后重新启动，可使用 `--arm-timeout 120`。先检查蓝牙连接，空闲时没有报告本身不说明设备损坏。
3. **映射似乎没生效**：确认终端已出现 `Mapping active`，进程没有因时限、断连或错误退出；同时确认没有另一个实例。修改配置后必须重启。
4. **滚轮没明显效果**：把鼠标光标移到聊天记录、长网页或其他确实可以滚动的区域，再转动滚轮。终端统计与当前窗口能否滚动是两项不同检查。

可选桌面测试需在图形会话运行，并满足 GTK/PyGObject 环境要求；它只观察窗口收到的事件，不能替代设备权限配置。在另一终端执行：

```bash
cd /home/longxiaoze/codes/rgr06-tools
/usr/bin/python3 rgr06_desktop_test.py --stage wheel --seconds 120 --output evidence/my-new-wheel-window.jsonl
```

`--stage` 可选 `left`、`right`、`middle`、`wheel`、`touch`、`all`。窗口时限是整数 `1..120` 秒；每次指定新的输出文件。先确认映射处于激活状态，再依照窗口提示测试。默认触控板模式下，`touch` 阶段用肉眼确认鼠标箭头移动；该窗口不统计普通光标移动，触摸计数保持 0 属于预期。触摸屏备选模式则可检查窗口的触摸事件计数。

实际参数可随时自查：

```bash
python3 rgr06_mapper.py --help
python3 rgr06_capture.py record --help
/usr/bin/python3 rgr06_desktop_test.py --help
```
