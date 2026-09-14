---
name: rehab-exercise
description: 康复运动操作指南
---

## 可用命令参考

| 命令 | 参数 | 默认值 | 可选值 | 说明 |
|------|------|--------|--------|------|
| `extend-hand` | `--offset` | mid | low / mid / high | 伸出手臂 |
| `raise-hand` | `--level` | significant | slight / moderate / significant | 抬起手 |
| `lower-hand` | `--level` | slight | slight / moderate / significant | 放下手 |
| `exec-trace` | `--type`(必填) | — | up_and_down | 轨迹类型 |
|  | `--speed` | normal | slow / normal / fast | 移动速度 |
|  | `--scale` | mid | short / mid / long | 移动幅度 |
|  | `--repeat` | 1 | 整数 | 重复次数 |
| `retract-hand` | — | — | — | 收回手臂 |
| `list-trace` | — | — | — | 列出可用轨迹 |
| `turn -t user\table` | — | — | — | 转向用户或桌面 |
| `observe_environment(duration=n)` | — | — | — | 观察环境，n=0 拍照，n>0 录制视频+音频 |

> **注意**：所有康复运动命令通过 `execute_shell` 执行，用户ID 来自声纹识别结果；若护工或管理员指示服务某位老人，则使用该老人的ID。

## 操作流程

### 第一步：确认用户状态
1. 用温和语气告知老人"我看下您的情况"，然后 `execute_shell("turn -t user")` 转向用户
2. `observe_environment()` 拍摄照片，画面已标注人脸检测结果（绿框=已注册用户，红框=未知人员），直接根据标注判断：
   - 画面中绿框标注了目标用户（框上名字与服务对象一致）→ 身份确认，继续下一步
   - 画面中无人或只有红框/陌生人 → 告知用户"请走到摄像头前面来"，重新执行本步
   - 目标用户不在画面中但检测到其他人 → 告知当前用户"请让<目标用户>到摄像头前来"，等待后重新执行
3. 身份确认后，观察照片中用户状态：
   - 坐姿端正、清醒 → 告知用户"我们来活动一下身体"，进入第二步
   - 躺着/半躺/歪斜/睡觉/精神萎靡/表情痛苦 → 告知用户"您现在状态不适合做康复运动"，停止操作，通知护工
   - 不确定 → 主动询问"您感觉怎么样？想做康复运动吗？"，根据回答判断

### 第二步：伸出手臂
1. `execute_shell("list-trace")` 列出可用轨迹
2. 根据用户需求选择合适的轨迹类型
3. 告知老人"我把手伸过来"，然后 `execute_shell("extend-hand --offset mid")` 伸出手臂

### 第三步：调整高度
1. 告知老人"我伸出手了"，然后 `observe_environment()` 拍照确认手臂与用户的相对位置
2. 询问用户"您觉得手的高度合适吗？需要抬高一点还是放低一点？"
3. 根据用户反馈调整：
   - 用户说抬高 → `execute_shell("raise-hand --level moderate")`，然后返回第2步再确认
   - 用户说放低 → `execute_shell("lower-hand --level moderate")`，然后返回第2步再确认
   - 用户说合适/可以 → 进入第四步


### 第四步：开始运动
1. 告知老人"来，请握住我的手"，然后 `execute_shell("sleep 3")` 等待用户握好
2. 告知老人"我们开始运动，做三次"，然后 `execute_shell("exec-trace --type up_and_down --speed slow --scale mid --repeat 3")` 执行轨迹运动
3. 运动过程中执行 `observe_environment(duration=3)` 观察用户反应：
   - 正常配合 → 运动完成后继续
   - 用户表情痛苦/不适/拒绝 → 立即停止，进入第五步
4. 运动完成后，告知老人"三次做完了，感觉怎么样？"
5. 询问用户"还要继续吗？"
   - 用户说继续/再来/再做 → 询问是否需要调整参数，然后返回第2步重新执行
   - 用户说不要/够了/累了 → 进入第五步

### 第五步：收回手臂
1. 告知老人"好的，我把手收回来"，然后 `execute_shell("retract-hand")` 收回手臂
2. `execute_shell("turn -t user")` 转向用户
3. 询问用户是否需要其他帮助

## 特别注意
- **必须先通过 `observe_environment()` 确认身份和状态**，身份不匹配或状态异常绝不进行康复运动
- **执行 `extend-hand`、`raise-hand`、`lower-hand` 后，必须立即调用 `observe_environment` 观察用户状态，不可跳过**
- **执行 `exec-trace` 后，必须立即调用 `observe_environment` 观察用户反应，不可跳过**
- 用户表示疼痛、不适或拒绝 → 立即 `retract-hand` 收回手臂，不继续追问
- 默认运动参数应为缓和的（speed=slow, scale=mid, repeat=3），除非用户主动要求调整
- 每次运动完成后必须先询问用户是否继续，不自动连续运动