# GPS 轨迹坐标系修复

## 核心功能

本项目用于**自动修复 GPS 轨迹中的坐标系混用问题**。在实际数据采集中，设备可能混合使用不同坐标系（WGS-84 和 GCJ-02），导致轨迹出现"跳跃"或"抖动"。该工程智能识别并纠正这些异常点。但是仍然有一些情况无法修复

### 主要成果
- ✅ 自动检测坐标系跳变（距离突变）
- ✅ 智能修复异常点（GCJ-02 → WGS-84 转换）
- ✅ 方向连续性约束（避免不合理的转向）
- ✅ 前瞻决策机制（利用后续点优化当前决策）
- ✅ 交互式地图可视化（修复前后对比）
![轨迹修复前后对比](example.png)
---

## 设计思路

### 问题背景
GPS 设备在中国大陆通常输出 GCJ-02 坐标（高德、腾讯使用），但国际标准是 WGS-84。当设备或应用混用这两种坐标系时，轨迹会出现：
- **异常跳变**：相邻两点距离异常大（>50m）
- **方向突变**：转向角异常小（<60°）
- **路线不合理**：修复后反而距离更远

### 核心算法

#### 1. **跳变检测**
```
如果 相邻两点距离 > JUMP_DETECT_THRESHOLD（50m）
  → 疑似坐标系混用，尝试修复
```

#### 2. **备选坐标转换**
对当前点进行逆向纠偏：
```
GCJ-02 坐标 →[逆向数学变换]→ WGS-84 坐标
```

#### 3. **合理性判断**（三层验证）

| 条件 | 含义 | 权重 |
|------|------|------|
| **cond_jump** | 原始跳变 > 50m | 必要条件 |
| **cond_smooth** | 修复后 < 800m | 必要条件 |
| **cond_improve** | 改善距离 > 4m | 基础阈值 |
| **锐角检查** | 转向角 < 60° 时，修复门槛 ×50 | 防护条件 |
| **前瞻决策** | 看第三个点的路径长度 | 不确定时的决策 |

#### 4. **方向连续性约束**
修复不能导致"不合理的转向"：
- 计算修复前后的转向角
- 若修复导致锐角（<60°），提高修复门槛
- 防止为了近距离而产生 Z 字形轨迹

#### 5. **前瞻决策机制**
在模糊区间（|improvement| < 120m）时：
```
对比两条路径总长度：
  路径 A（不修）= dist(last→curr_raw) + dist(curr_raw→next)
  路径 B（修）   = dist(last→curr_fix) + dist(curr_fix→next)
  
若 路径 B 显著更短 → 倾向修复
```

---

## 运行流程

```
输入数据
   ↓
[1] cut.py  ← 截取时间段或行号范围
   ↓
./output/cut.csv (原始片段)
   ↓
[2] run.py  ← 智能修复坐标系跳变
   ↓
./output/gps_data_perfect.csv (修复后的数据)
./output/debug_decisions.csv   (决策日志)
   ↓
[3] plot.py ← 交互式地图对比
   ↓
trajectory_before_after.html (可视化结果)
```

### 第一步：数据截取 (cut_gps_data.py)

**目的**：从原始数据中提取感兴趣的时间段

**配置项**：
```python
MODE = "time"           # "line"(按行) 或 "time"(按时间)
START_TIME = "2022-03-17 00:00:00"
END_TIME   = "2025-03-20 23:59:00"
TIMEZONE_OFFSET = 2     # UTC 偏移
```

**输出**：
- `./output/cut.csv` - 截取后的原始 GPS 数据

---

### 第二步：智能修复 (run.py)

**目的**：自动检测并纠正坐标系混用问题

**核心参数**：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `JUMP_DETECT_THRESHOLD` | 50.0 m | 跳变判定阈值 |
| `SMOOTH_THRESHOLD` | 800.0 m | 修复后合理性上限 |
| `MIN_IMPROVEMENT` | 4.0 m | 基础修复收益 |
| `AMBIGUOUS_THRESHOLD` | 120.0 m | 启用前瞻的不确定区 |
| `SHARP_TURN_DEG` | 60.0 ° | 锐角定义 |
| `SHARP_GAIN_MULTIPLIER` | 50 | 锐角时修复门槛倍数 |

**处理流程**：
1. 按 `geoTime` 排序 + 去重
2. 逐点决策：修复 vs 保留原值
3. 输出修复日志

**输出**：
- `./output/gps_data_perfect.csv` - 新增列：
  - `clean_longitude` - 修复后的经度
  - `clean_latitude` - 修复后的纬度
  - `repair_note` - 修复决策说明
- `./output/debug_decisions.csv` - 详细决策日志（调试用）

---

### 第三步：可视化 (plot.py)

**目的**：在交互式地图上对比修复前后的轨迹

**图层说明**：
- 🔴 **红色线** - 原始轨迹（坐标系混用）
- 🔵 **蓝色线** - 修复后轨迹（统一 WGS-84）
- 🔵 **蓝色点** - 修复点位置 + geoTime 标签
- 🟢 **绿色标记** - 起点
- 🔴 **红色标记** - 终点

**输出**：
- `trajectory_before_after.html` - 可在浏览器中打开的交互式地图

---

## 快速开始

### 1. 环境准备
```bash
pip install pandas folium numpy
```

### 2. 准备数据
将原始 CSV 文件放在 `./data/` 目录，修改 `cut_gps_data.py` 中的 `input_csv` 路径。

### 3. 执行流程
```bash
# 步骤 1：截取数据
python cut_gps_data.py

# 步骤 2：智能修复
python run.py

# 步骤 3：生成地图
python plot.py

# 查看结果
open trajectory_before_after.html
```

---

## 输出文件说明

| 文件 | 格式 | 用途 |
|------|------|------|
| `cut.csv` | CSV | 原始片段（中间产物） |
| `gps_data_perfect.csv` | CSV | 修复后的完整数据 |
| `debug_decisions.csv` | CSV | 每个点的决策详情（调试） |
| `trajectory_before_after.html` | HTML | 交互式地图（最终产物） |

---

## 调试与优化

### 查看单个点的决策过程
打开 `debug_decisions.csv`，关键字段：
- `decision` - 最终决策（REPAIRED / ORIGINAL / LOOKAHEAD_FIX 等）
- `improvement` - 修复改善的距离（米）
- `sharp_turn` - 是否存在锐角限制
- `angle_prev_raw / angle_prev_fix` - 修复前后的转向角

### 调整参数
如果修复效果不理想：
1. **过度修复**（错误地修改了本来正确的点）
   → 增大 `MIN_IMPROVEMENT` 或 `SHARP_GAIN_MULTIPLIER`

2. **遗漏修复**（没有修复应该修复的点）
   → 降低 `JUMP_DETECT_THRESHOLD` 或 `AMBIGUOUS_THRESHOLD`

3. **不合理转向**（修复导致方向突变）
   → 降低 `SHARP_TURN_DEG`（更严格的角度检查）

---

## 坐标系说明

- **WGS-84**：国际标准（GPS）
- **GCJ-02**：中国加密坐标系（高德、腾讯、百度使用）
- **转换原理**：本项目使用逆向纠偏公式 `GCJ-02 → WGS-84`

---

## 已知限制

1. 仅支持中国大陆坐标（GCJ-02 转换区域限制）
2. 假设坐标跳变是系统性的（连续段使用同一坐标系）
3. 无法处理 GPS 信号严重缺失的情况
4. 前瞻决策需要至少 2 个后续点

---

## 联系方式

如有问题，请检查：
- ✅ 输入 CSV 是否包含 `latitude`, `longitude`, `geoTime` 列
- ✅ 输出目录 `./output` 是否存在且有写权限
- ✅ 参数配置是否符合实际数据特征
