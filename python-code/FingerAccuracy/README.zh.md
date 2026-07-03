# FingerAccuracy

基于摄像头 + MIDI,识别"弹钢琴时是哪个手指按了哪个键"。

[English (README.md)](README.md)

## 这个模块在整个项目里的位置

这个是一个更大的钢琴练习/学习实验里"判断到底是哪个手指按了这个键"的那一块。
大致的数据流是这样的:

```
提示出现 -> 受试者按下琴键
  -> MIDI 记录按下的键和时间戳 -> 摄像头判断用的哪个手指 -> 事后再做分析
```

这个仓库只管摄像头这一块,但设计上是想让它能被整条流程调用——不管是实验
进行中实时调用,还是事后拿一段录好的视频调用,都可以,具体见下面的
[作为库来调用](#作为库来调用)。

## 工作原理

一共三步,前两步每套设备只用做一次:

1. 标定:在摄像头画面上点击,像颜料桶填色一样(以 Canny 边缘检测出的线为
   "墙")圈出每个键精确的像素范围。存成一个 *profile*:
   `data/<profile名字>/keyboard_template.json` + `keyboard_key_map.png`(一张
   像素图,每个像素的值就是它属于第几号键,0 表示不属于任何键)。
2. 把 MIDI 音符和这些键对应起来:按 1、2、3... 的顺序依次按下每个键,程序
   记下每个键实际发出的 MIDI 音符号,存进同一个 profile 文件夹里的
   `midi_mapping.json`。
3. 实时识别:MediaPipe 逐帧识别手部关键点、算出每根指尖的像素坐标。收到
   MIDI 音符事件时,先查出这是哪个键,再看哪根指尖的像素坐标落在这个键的
   范围内(没有的话就用离这个键最近的那根),这就是识别出来的手指。

左手大拇指到小拇指依次是 `L1`...`L5`;右手大拇指到小拇指依次是 `R1`...`R5`。
画面里出现 0 只、1 只还是 2 只手都没问题,不要求两只手同时在画面里。

一个 profile 只在"摄像头和键盘的相对位置不变"的前提下有效,挪动了就重新
跑一遍第一步。

## 环境准备

```bash
conda activate fingercam    # 或者: conda env create ...; pip install -r requirements.txt
```

所有脚本都在 `fingercam` 这个 conda 环境里跑(Python、OpenCV、MediaPipe、mido、
PySide6)。MediaPipe 的模型文件 `hand_landmarker.task` 已经放在这个文件夹里了。

## 主流程脚本

以下命令都要在 `python-code/FingerAccuracy/` 目录下运行。

| 脚本 | 作用 |
|---|---|
| `step1_keyboard_wizard.py` | PyQt 向导——标定一个新 profile(全程点击操作,不需要按键盘快捷键)。 |
| `step2_midi_mapping.py` | PyQt 向导——按 1..N 的顺序按键,记录每个键对应的 MIDI 音符。 |
| `main.py` | 实时主程序——键盘彩色框 + 手部骨架 + 实时手指/按键匹配。 |
| `demo_keyboard_preview.py` | 演示/工具脚本——下拉框选 profile,对着摄像头实时核对标定得准不准,也可以切换显示 MIDI 音符名而不是键编号。 |

```bash
python step1_keyboard_wizard.py
python step2_midi_mapping.py
python main.py
```

每个脚本开头的注释里有更详细的说明;`config.json`(第一次运行会自动生成)
存的是摄像头编号/翻转设置、Canny 阈值、MIDI 端口,以及当前"激活"的是哪个
profile(脚本不需要你手动选的时候,默认就用这个)。

## Profile 文件夹结构

```
data/<profile名字>/
  keyboard_template.json   # 每个键的 id、种类(白键/黑键)、note(通常是空的)
  keyboard_key_map.png     # 像素图:值 = key_id + 1,0 = 背景
  midi_mapping.json        # key_id -> MIDI 音符号(还有音符名),来自第二步
```

可以同时存在多个 profile(对应不同的桌子、摄像头、键盘);每个工具都有下拉框
或者读 `config.json` 里的 `active_profile` 来决定用哪一个。

## 作为库来调用

这里的功能都没有跟 PyQt 界面绑死——`fingeraccuracy/gui/` 里的界面只是对
顶层包里那些普通函数/类的一层薄封装。

```python
from fingeraccuracy import (
    Config, Camera, HandTracker, MidiListener,
    KeyboardTemplate, MidiMapping, match_note_to_finger,
)

cfg = Config.load()
template = KeyboardTemplate.load(f"data/{cfg.active_profile}/keyboard_template.json")
mapping = MidiMapping.load(f"data/{cfg.active_profile}/midi_mapping.json")

tracker = HandTracker()
midi = MidiListener()          # 不指定端口名就自动用第一个可用的

with Camera(cfg.camera) as cam:
    while True:
        frame = cam.read()
        hands = tracker.process(frame)          # {"Left": Hand(...), "Right": Hand(...)}

        for event in midi.pop_events():         # MidiEvent(time, note)
            match = match_note_to_finger(event.note, template, mapping, hands)
            if match:
                print(match.finger, "按了第", match.key_id + 1, "号键, 音符", event.note)
```

### 先录下来,后期再分析

`MidiListener` 会给每个事件打时间戳(从这个 listener 启动开始算的秒数——
和 `HandTracker` 自己视频时间戳的计时方式是一致的),所以可以先把一次实验的
MIDI 记录下来,事后再拿一段录好的视频去匹配,而不必实时处理:

```python
from fingeraccuracy import MidiListener, save_midi_log, analyze_recording

midi = MidiListener()
# ... 做你的实验,同时比如用 cv2.VideoWriter 把同一个摄像头的画面录下来 ...
save_midi_log(midi.pop_events(), "session1_notes.json")

# 之后,不需要摄像头也不需要连 MIDI 设备:
results = analyze_recording(
    video_path="session1.mp4",
    midi_log_path="session1_notes.json",
    profile_name="desk_webcam",
)
for r in results:
    print(r)   # FingerMatch(note=..., key_id=..., finger=..., inside=...) 或者 None
```

`video_path` 和 `midi_log_path` 的时间起点必须是同一个(视频和 `MidiListener`
要在同一时刻开始录制),因为每个 MIDI 事件都是靠这个共享的时间戳去视频里找
对应时刻的手部位置的。

`Camera` 也可以直接传一段录好的视频文件路径来代替实时摄像头编号
(`CameraConfig.index` 两种都接受)——如果你想用跟实时一样的代码逻辑去"回放"
一段录像,而不是用 `analyze_recording`,这个也能用。

## 项目目录结构

```
main.py                        # 大致对应"第三步":实时指法识别主程序(入口)
step1_keyboard_wizard.py       # 第一步:标定向导(入口)
step2_midi_mapping.py          # 第二步:MIDI 映射向导(入口)
demo_keyboard_preview.py       # 工具:profile 查看器(入口)
config.json                    # 摄像头/检测/向导/MIDI 设置 + active_profile
hand_landmarker.task           # MediaPipe 手部关键点模型
data/<profile>/...             # 标定出来的 profile(见上面)
archive/                       # 更早期的原型脚本,只作参考保留

fingeraccuracy/
  camera.py                    # Camera —— cv2.VideoCapture 的封装(摄像头编号或视频文件都行)
  config.py                    # Config / 各种 *Config 数据类,负责 JSON 读写
  hand_tracking.py             # HandTracker、Hand —— MediaPipe 封装,输出 L1-L5/R1-R5 指尖坐标
  midi.py                      # MidiListener、MidiEvent、list_input_ports、save/load_midi_log
  finger_matching.py           # match_note_to_finger、FingerMatch —— 核心匹配逻辑
  offline.py                   # analyze_recording —— 录好的视频 + MIDI 记录 -> 匹配结果
  profiles.py                  # list_profiles —— 扫描 data/<名字>/ 文件夹
  keyboard/
    template.py                # KeyBox、KeyboardTemplate —— 像素级精确的按键地图
    wizard.py                  # KeyFillWizard —— 颜料桶式按键分割
    midi_mapping.py            # MidiMapping —— key_id 与 MIDI 音符的对应关系
    visualize.py                # 给 key_map 上色/画标签的小工具函数
    detector.py                 # 标定向导用来做 Canny 边缘检测
  gui/                          # 上面四个脚本用到的 PyQt 窗口/页面
```
