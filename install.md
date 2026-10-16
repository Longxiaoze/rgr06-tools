# 安装与权限准备

这些脚本直接在本目录运行，核心只用 Python 标准库，无 pip 或 npm 依赖。使用已有的 **Python 3.10 或以上版本**。实现支持 64 位 Linux 的 `x86_64` / `aarch64` ioctl ABI；实际设备和桌面验证仅覆盖本机 Ubuntu、GNOME、Wayland，未验证其他系统组合。

功能与限制见 [readme.md](readme.md)，日常运行和自定义见 [run.md](run.md)。本工具不安装软件或系统服务，也不配置 root 自启。

## 先检查现有环境

以下命令只检查，不安装或修改权限：

```bash
cd /home/longxiaoze/codes/rgr06-tools
command -v python3
python3 --version
uname -sm
command -v getfacl
command -v setfacl
command -v sudo
```

ACL 操作需要系统已有 `getfacl`、`setfacl`。若缺少这些命令，请先查阅你的系统文档并由你决定如何准备；这里没有自动安装步骤。不要以 root 身份启动映射器。`sudo` 密码只在你自己的终端输入，不发到聊天或写入文件。

## 配对并确认这台 RGR06

先在系统蓝牙界面正常配对、连接设备。当前适配对象是：

- 蓝牙 HID bus：`0005`，VID:PID：`248a:045b`。
- 唯一地址：`d1:05:cb:5c:37:15`。
- 已固定的 HID 描述符 SHA-256：`177ec423b4adb8fcd2137ed9476f5d61baa261950aa93b9cb33d25e7f4be02e6`。

```bash
python3 rgr06_capture.py inspect
```

核对输出的 `identity`、`descriptor.known_decoder` 和 `nodes`。正常应发现两个 `evdev`、一个 `hidraw` 节点。`event26`、`event27`、`hidraw3` 只是最初会话的编号，**重连后必须重新确认，不能照抄旧编号授权**。运行时还会在打开节点后通过 ioctl 再验证设备身份。

目标地址和描述符约束写在代码中；其他同型号设备不能只修改 JSON 配置就使用，需先独立确认身份、描述符和固件动作。

## 最小输入权限

本机当前三个输入节点已经获得只读 ACL；如果 `inspect` 显示都可读，直接跳到下一节。下面是供你在终端手动执行的准备步骤。它重新通过工具的完整身份发现节点，只给当前尚不可读、且没有当前用户命名 ACL 的目标节点添加只读条目；遇到已有条目则停止，避免覆盖原有权限。

授权期间保持设备连接。整段在 Bash 子 shell 中执行，失败只终止这段操作：

```bash
(
  set -euo pipefail
  cd /home/longxiaoze/codes/rgr06-tools
  rgr06_uid="$(id -u)"
  mapfile -t rgr06_nodes < <(python3 - <<'PY'
from rgr06_capture import discover, DESCRIPTOR_SHA256
inspection = discover()  # 按完整 bus/VID/PID/唯一地址选择，歧义时拒绝。
nodes = inspection["nodes"]
if inspection["descriptor"]["sha256"] != DESCRIPTOR_SHA256:
    raise SystemExit("描述符不匹配；停止授权")
if len(nodes) != 3 or sum(n["kind"] == "evdev" for n in nodes) != 2 or sum(n["kind"] == "hidraw" for n in nodes) != 1:
    raise SystemExit("不是两个 evdev 加一个 hidraw；停止授权")
if not all(n["exists"] for n in nodes):
    raise SystemExit("有节点已经消失；重新连接并检查")
for node in nodes:
    print(node["path"])
PY
  )
  if [[ "${#rgr06_nodes[@]}" -ne 3 ]]; then
    printf '%s\n' '未取得完整目标节点列表，停止。' >&2
    exit 1
  fi
  printf '当前 RGR06 节点：%s\n' "${rgr06_nodes[@]}"
  rgr06_add=()
  for rgr06_node in "${rgr06_nodes[@]}"; do
    if [[ ! -r "$rgr06_node" ]]; then
      rgr06_acl="$(getfacl -ncp -- "$rgr06_node")"
      if python3 -c 'import sys; prefix="user:"+sys.argv[1]+":"; sys.exit(0 if any(line.startswith(prefix) for line in sys.stdin) else 1)' "$rgr06_uid" <<< "$rgr06_acl"; then
        printf '已有当前用户 ACL，先核对有效权限和 mask：%s\n' "$rgr06_node" >&2
        exit 1
      fi
      rgr06_add+=("$rgr06_node")
    fi
  done
  if [[ "${#rgr06_add[@]}" -eq 0 ]]; then
    printf '%s\n' '三个目标节点已可读，无需更改。'
    exit 0
  fi
  mkdir -p evidence
  rgr06_backup="$(mktemp -d "$PWD/evidence/acl-before.XXXXXX")"
  getfacl -p -- "${rgr06_add[@]}" > "$rgr06_backup/input.acl"
  printf '%s\n' "${rgr06_add[@]}" > "$rgr06_backup/added-read-nodes.txt"
  printf '权限备份：%s\n' "$rgr06_backup"
  sudo setfacl --no-mask -m "u:$rgr06_uid:r--" -- "${rgr06_add[@]}"
)
```

数组元素始终被引号包围，文件操作使用 `--`，不把设备路径作为 shell 代码执行。若设备在确认后断连、节点被复用，停止并从 `inspect` 重新确认。`--no-mask` 保留已有 mask，避免重新计算时意外扩大其他条目的有效权限；新增 mask 时会采用原有组权限。若新增条目仍受 mask 限制，先查看 `getfacl` 并逐项核对影响，不要盲目扩大 mask。已有用户条目仍不可读时，也应保留其原权限再检查。

## `/dev/uinput` 权限

映射器以 `O_WRONLY` 打开 `/dev/uinput`，因此最小需求是写权限。本机本次工作开始前已经有读写 ACL，**直接保留，不需要重复添加，也不能把既有条目当作本工具新增条目撤销**。

先只读检查：

```bash
getfacl -p -- /dev/uinput
[[ -w /dev/uinput ]] && printf '%s\n' 'uinput 当前可写，无需更改'
```

只有确实缺少写权限时才处理。下例仅在当前用户没有命名 ACL 条目时添加写权限；已有条目时停止，先保留现有权限并按需补充 `w`。所有更改都应先保存原状：

```bash
(
  set -euo pipefail
  cd /home/longxiaoze/codes/rgr06-tools
  if [[ -w /dev/uinput ]]; then
    printf '%s\n' 'uinput 已可写，无需更改。'
    exit 0
  fi
  rgr06_uid="$(id -u)"
  rgr06_acl="$(getfacl -ncp -- /dev/uinput)"
  if python3 -c 'import sys; prefix="user:"+sys.argv[1]+":"; sys.exit(0 if any(line.startswith(prefix) for line in sys.stdin) else 1)' "$rgr06_uid" <<< "$rgr06_acl"; then
    printf '%s\n' '已有当前用户 ACL，先保留原权限并核对 mask，再按需补充 w。' >&2
    exit 1
  fi
  mkdir -p evidence
  rgr06_uinput_backup="$(mktemp "$PWD/evidence/uinput-before.XXXXXX.acl")"
  getfacl -p -- /dev/uinput > "$rgr06_uinput_backup"
  printf 'uinput 原权限备份：%s\n' "$rgr06_uinput_backup"
  sudo setfacl --no-mask -m "u:$rgr06_uid:-w-" -- /dev/uinput
)
```

不要加入全局 `input` 组、不要 `chmod 666`，也不要扩大到其他设备节点。以上步骤不加载模块、不创建 `/dev/uinput`；节点不存在时先查你的系统文档。

准备好后检查：

```bash
python3 rgr06_mapper.py --check
```

`--check` 验证目标身份、描述符与权限，短暂等待目标报告；不抓取输入、不创建虚拟设备。检查通过后按 [run.md](run.md) 启动。

## 撤销本次添加的 ACL

先停止映射器。仅撤销你这次确实新增、且当前仍属于这台 RGR06 的用户条目。上面的 `added-read-nodes.txt` 记录了尝试添加的输入路径；重新运行 `inspect` 核对身份和路径后，才对其中每个仍匹配的节点执行：

```bash
# 将下面路径替换为已再次确认属于 RGR06、且本次确实新增了条目的单个节点。
sudo setfacl --no-mask -x "u:$(id -u)" -- /dev/input/已确认的event编号
```

若需恢复 ACL mask 等完整原状，可以使用 `sudo setfacl --restore=你的备份文件`，但必须先确认备份中的设备路径仍指向原来的设备、且此后没有其他合法权限变化。**重连后旧备份可能指向别的设备，不能直接恢复**。已有用户条目的权限若曾被调整，应在核对后恢复原权限，不能删除整个条目。

`/dev/uinput` 同理：只有这次从无到有添加了用户条目时才可删除；原来已有 ACL 就保留，或按已核对的备份恢复。保留备份用于检查，不自动撤销或覆盖其他权限。

## 可选桌面接收测试窗

核心工具不依赖 GTK。`rgr06_desktop_test.py` 是可选验证窗，需要 Python 的 `gi` 与 GTK 3；本机 `/usr/bin/python3` 已有这些依赖。只读检查：

```bash
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk; print(Gtk.get_major_version(), Gtk.get_minor_version())'
```

无需为核心运行安装 GUI 依赖；测试窗的使用方法见 [run.md](run.md)。
