# ForecastEye

基于 **statsforecast** 的轻量级时序异常检测工具，用于 Prometheus + ClickHouse 监控指标分析。

## 特性

- **轻量级**：纯统计模型，无需 GPU，毫秒级推理
- **易于部署**：pip install 即可使用，依赖少
- **时序感知**：自动处理日周期、周周期等季节性模式
- **异常评分**：输出标准化的异常分数，便于告警配置

## 快速开始

### 1. 安装依赖

```bash
pip install statsforecast clickhouse-connect pandas pyyaml
```

### 2. 配置

```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml，填入 ClickHouse 连接信息
```

### 3. 初始化结果表

```bash
clickhouse-client --host your-host --port 9002 \
  -q "CREATE TABLE IF NOT EXISTS monitor.anomaly_detection_results (...)"
```

### 4. 运行检测

```bash
# 试运行（不写入）
python statsforecast_anomaly_detector.py --config config.yaml --dry-run

# 正式运行
python statsforecast_anomaly_detector.py --config config.yaml
```

### 5. 查询结果

```sql
-- 查询最近的异常
SELECT host, metric_name, anomaly_score, actual_value, predicted_value
FROM monitor.anomaly_detection_results
WHERE is_anomaly = 1
ORDER BY anomaly_score DESC;

-- 查询异常统计
SELECT metric_name, countIf(is_anomaly=1) as anomaly_count
FROM monitor.anomaly_detection_results
WHERE timestamp > now() - INTERVAL 24 HOUR
GROUP BY metric_name;
```

## 配置说明

```yaml
clickhouse:
  host: 'your-clickhouse-host'
  port: 8123                    # HTTP 端口
  database: 'monitor'

query:
  lookback_hours: 24            # 回溯时间
  filter: ""                    # 指标过滤条件

model:
  type: 'SeasonalNaive'         # 模型类型
  freq: '1min'                  # 采样频率
  season_length: [1440]         # 日周期（24h × 60min）
  forecast_horizon: 10          # 验证集大小
  write_increment: 5            # 写入增量（与定时任务频率匹配）

threshold:
  score_threshold: 1.5          # 异常阈值
  min_samples: 50               # 最小样本数
```

## 异常分数解读

```
anomaly_score = |实际值 - 预测值| / (置信区间半宽度)
```

| 阈值 | 含义 |
|------|------|
| < 1.0 | 正常 |
| 1.0~1.5 | 观察 |
| > 1.5 | 异常（触发告警） |

## 模型选择

| 模型 | 适用场景 | 性能 |
|------|----------|------|
| SeasonalNaive | 快速检测，数据平稳 | ★★★★★ |
| MSTL | 多周期（日+周），计算量较大 | ★★★ |
| AutoETS | 通用时序 | ★★★★ |
| AutoARIMA | 自回归场景 | ★★★ |

## 定时任务

```bash
# 每 5 分钟执行一次
*/5 * * * * cd /path/to/ForecastEye && python statsforecast_anomaly_detector.py --config config.yaml >> /var/log/anomaly.log 2>&1
```

## 文件说明

| 文件 | 说明 |
|------|------|
| statsforecast_anomaly_detector.py | 主检测脚本 |
| config.yaml.example | 配置模板 |
| init_clickhouse_tables.sql | SQL 初始化脚本 |
| requirements.txt | Python 依赖 |
| DESIGN.md | 设计文档 |

## 许可证

MIT License