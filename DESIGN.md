# ForecastEye - Prometheus + ClickHouse 时序异常检测方案设计

## 1. 概述

### 1.1 目标
替代原有的 Google 时序模型方案，使用更轻量、更简单的 statsforecast 开源工具实现 Prometheus 指标异常检测。

### 1.2 原方案 vs 新方案对比

| 维度 | Google 时序模型 | StatsForecast |
|------|-----------------|---------------|
| 部署复杂度 | 需 TensorFlow/JAX，GPU 可选 | 仅需 Python + pip install |
| 模型大小 | 大（需大量内存） | 极小（纯统计模型） |
| 推理速度 | 较慢（秒级） | 极快（毫秒级，C++ 核心） |
| 季节性处理 | 手动配置 | 自动检测多周期 |
| 维护难度 | 高（依赖重） | 低（依赖少） |
| 开源程度 | 部分需要 license | 完全开源 Apache 2.0 |

---

## 2. 系统架构

### 2.1 整体流程图

```
┌─────────────┐     ┌──────────────────────┐     ┌───────────────────┐
│  Prometheus │────▶│  monitor.metrics     │────▶│  Python 脚本      │
│   (采集)    │     │  (ClickHouse 存储)   │     │  (statsforecast)  │
└─────────────┘     └──────────────────────┘     └───────────────────┘
                                                         │
                                                         ▼
                                                  ┌────────────────────────┐
                                                  │ monitor.anomaly_       │
                                                  │ detection_results      │
                                                  └────────────────────────┘
                                                         │
                                                         ▼
                                                  ┌──────────────┐
                                                  │  Grafana     │
                                                  │ (可视化告警) │
                                                  └──────────────┘
```

### 2.2 数据流详解

```
1. Prometheus 定期采集指标 → monitor.metrics (ClickHouse)
   - 表结构: ts, nename, host, metric_name, metric_value, label
   
2. 定时脚本触发异常检测
   - 每 5 分钟运行一次
   
3. Python 脚本执行（交叉验证模式）：
   a) 从 monitor.metrics 读取最近 24 小时数据
   b) 构建 unique_id = nename#host#metric_name#label（使用#分隔符）
   c) 每个时序保留最近10个点作为验证集
   d) 用 SeasonalNaive（日周期1440）预测验证集
   e) 计算异常分数 = |实际-预测| / 置信区间半宽度
   f) 将结果写回 monitor.anomaly_detection_results
   
4. Grafana 通过 ClickHouse 数据源展示异常
   - 异常分数仪表盘
   - 异常事件列表
   - 告警规则（anomaly_score > 1.5）
```

---

## 3. ClickHouse 表结构设计

### 3.1 原始指标表（已存在）

```sql
CREATE TABLE monitor.metrics
(
    `ts` DateTime,           -- 时间戳
    `nename` String,         -- 网元名称
    `host` String,           -- 主机名
    `metric_name` String,    -- 指标名
    `metric_value` Float64,  -- 指标值
    `label` String           -- 标签
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (ts, nename, host, metric_name, label)
TTL ts + toIntervalDay(365)
```

### 3.2 异常检测结果表（新建）

```sql
CREATE TABLE monitor.anomaly_detection_results (
    timestamp DateTime,              -- 异常发生时间（对应 ts）
    nename String,                   -- 网元名称
    host String,                     -- 主机名
    metric_name String,              -- 指标名
    label String,                    -- 标签
    actual_value Float64,            -- 实际值 (metric_value)
    predicted_value Float64,         -- 预测值
    anomaly_score Float64,           -- 异常分数（核心指标）
    direction Int8,                  -- 方向：1=高于预测，-1=低于预测
    is_anomaly UInt8,                -- 是否异常：0/1
    detected_at DateTime,            -- 检测时间
    model_name String DEFAULT 'MSTL' -- 模型名称
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (nename, host, metric_name, timestamp);
```

### 3.3 字段映射关系

| monitor.metrics | statsforecast | anomaly_detection_results |
|-----------------|---------------|---------------------------|
| ts | ds | timestamp |
| nename | unique_id（部分） | nename |
| host | unique_id（部分） | host |
| metric_name | unique_id（部分） | metric_name |
| metric_value | y | actual_value |
| label | unique_id（部分） | label |

**unique_id 构建规则**:
```
unique_id = concat(nename, '#', host, '#', metric_name, '#', label)
示例: nename#hostname#cpu_utilization#default
```

---

## 4. StatsForecast 模型选择指南

### 4.1 模型对比

| 模型 | 适用场景 | 速度 | 季节性处理 | 推荐指数 |
|------|----------|------|-----------|---------|
| **MSTL** | 运维指标（多周期） | ★★★★ | 自动多周期 | ★★★★★ |
| AutoETS | 通用时序 | ★★★ | 自动单周期 | ★★★★ |
| AutoARIMA | 自回归场景 | ★★★ | 手动配置 | ★★★ |
| SeasonalNaive | 简单快速检测 | ★★★★★ | 单周期 | ★★★ |

### 4.2 推荐配置

**CPU/内存使用率**（日周期 + 周周期，工作日/周末差异）
```yaml
model:
  type: MSTL
  freq: '5min'
  season_length: [288, 2016]  # 日周期288点，周周期2016点
```

**网络流量/请求量**（日周期为主）
```yaml
model:
  type: MSTL
  freq: '5min'
  season_length: [288, 2016]
```

**响应时间/延迟**（可能有突发峰值）
```yaml
model:
  type: MSTL
  freq: '1min'
  season_length: [1440, 10080]
```

---

## 5. 部署方案

### 5.1 直接部署

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 初始化结果表
clickhouse-client --host your-host < init_clickhouse_tables.sql

# 3. 配置 config.yaml（修改 ClickHouse 连接信息）

# 4. 设置定时任务
*/5 * * * * python /path/to/statsforecast_anomaly_detector.py --config config.yaml
```

### 5.2 Kubernetes 部署

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: anomaly-detector
spec:
  schedule: "*/5 * * * *"  # 每5分钟
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: detector
            image: python:3.11-slim
            command:
            - python
            - /app/statsforecast_anomaly_detector.py
            - --config
            - /app/config.yaml
            volumeMounts:
            - name: config
              mountPath: /app
          volumes:
          - name: config
            configMap:
              name: anomaly-detector-config
```

---

## 6. 异常分数解读

### 6.1 计算公式

```
anomaly_score = |actual - predicted| / (confidence_interval_half_width)

示例：
- predicted = 70%
- actual = 85%
- 95%置信区间 = [65%, 75%]
- interval_half_width = (75 - 65) / 2 = 5
- anomaly_score = |85 - 70| / 5 = 3.0

解释：实际值偏离预测值3个置信区间宽度，属于明显异常
```

### 6.2 阈值建议

| anomaly_score | 异常程度 | 建议动作 |
|---------------|----------|----------|
| 0 - 1.0 | 正常范围 | 无需处理 |
| 1.0 - 2.0 | 软异常 | 观察，记录日志 |
| 2.0 - 3.0 | 中度异常 | 发送告警，人工排查 |
| > 3.0 | 严重异常 | 紧急告警，立即处理 |

---

## 7. Grafana 配置

### 7.1 ClickHouse 数据源

```
Type: ClickHouse
Host: your-clickhouse-host
Database: monitor
```

### 7.2 异常仪表盘查询

**异常分数面板**:
```sql
SELECT 
    nename, host, metric_name,
    anomaly_score
FROM monitor.v_recent_anomalies
ORDER BY anomaly_score DESC
LIMIT 20;
```

**异常趋势图**:
```sql
SELECT 
    timestamp,
    anomaly_score
FROM monitor.anomaly_detection_results
WHERE nename = $nename
  AND host = $host
  AND metric_name = $metric_name
  AND timestamp > now() - INTERVAL 24 HOUR
ORDER BY timestamp;
```

### 7.3 告警规则

```yaml
name: High Anomaly Score
condition:
  query: |
    SELECT max(anomaly_score) 
    FROM monitor.anomaly_detection_results 
    WHERE timestamp > now() - INTERVAL 10 MINUTE
    AND is_anomaly = 1
  threshold: 2.0
  
actions:
  - type: webhook
    message: |
      网元: $nename
      主机: $host
      指标: $metric_name
      异常分数: $anomaly_score
```

---

## 8. 性能优化建议

### 8.1 查询优化

```sql
-- 使用分区过滤（加速查询）
SELECT ... FROM monitor.metrics
WHERE ts >= now() - INTERVAL 168 HOUR
  AND nename = 'your-nename'  -- 按排序键过滤
```

### 8.2 批量处理

```yaml
model:
  n_jobs: 8  # 根据 CPU 核心数调整
```

### 8.3 数据采样（高频指标）

```sql
-- 对1分钟粒度数据聚合到5分钟
SELECT 
    nename, host, metric_name, label,
    toStartOfFiveMinutes(ts) as ds,
    avg(metric_value) as y
FROM monitor.metrics
...
```

---

## 9. 故障排查

### 9.1 常见问题

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| 数据为空 | lookback_hours 太短 | 设置为 168 小时（7天） |
| 预测失败 | 样本不足（<50点） | 增加 min_samples 或增加回溯时间 |
| 性能慢 | 时序数量太多 | 添加 filter 过滤特定网元/指标 |
| 异常分数全为0 | 数据过于平稳 | 降低 score_threshold |

### 9.2 调试命令

```bash
# 试运行模式
python statsforecast_anomaly_detector.py --config config.yaml --dry-run

# 查看数据量
clickhouse-client -q "
SELECT count(), uniqExact(nename), uniqExact(metric_name)
FROM monitor.metrics WHERE ts > now() - INTERVAL 168 HOUR
"
```

---

## 10. 参考资料

- StatsForecast 官方文档: https://nixtlaverse.nixtla.io/statsforecast/
- ClickHouse Python 客户端: https://clickhouse.com/docs/en/integrations/python
- monitor.metrics 表结构: `SHOW CREATE TABLE monitor.metrics`