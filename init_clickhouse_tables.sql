-- Prometheus + ClickHouse 时序异常检测表结构初始化
-- 适配 10.1.62.240 monitor.metrics 表
-- 
-- 使用说明:
--   clickhouse-client --host 10.1.62.240 --port 9002 --password 'xxx' < init_clickhouse_tables.sql
-- 
-- 注意: ClickHouse 不支持多语句，需逐条执行

-- ============================================
-- 1. 异常检测结果表
-- ============================================
-- 已创建，如需重建请先删除:
-- DROP TABLE IF EXISTS monitor.anomaly_detection_results;

CREATE TABLE IF NOT EXISTS monitor.anomaly_detection_results (
    timestamp DateTime,              -- 异常发生时间（对应原始指标时间 ts）
    nename String,                   -- 网元名称（bjclas1bebm）
    host String,                     -- 主机名（完整主机名）
    metric_name String,              -- 指标名称（28种指标之一）
    label String,                    -- 标签（网卡名/磁盘名/进程名等）
    actual_value Float64,            -- 实际观测值
    predicted_value Float64,         -- 模型预测值
    anomaly_score Float64,           -- 异常分数（>1.5 表示异常）
    direction Int8,                  -- 方向：1=高于预测，-1=低于预测
    is_anomaly UInt8,                -- 是否异常：0=正常，1=异常
    detected_at DateTime DEFAULT now(), -- 检测时间
    model_name String DEFAULT 'MSTL' -- 模型名称
) ENGINE = MergeTree()
PARTITION BY toYYYYMM(timestamp)
ORDER BY (nename, host, metric_name, timestamp)
SETTINGS index_granularity = 8192;

-- TTL 设置（365天）
-- ALTER TABLE monitor.anomaly_detection_results MODIFY TTL timestamp + toIntervalDay(365);

-- ============================================
-- 2. 常用查询视图
-- ============================================

-- 最近1小时异常视图
-- CREATE VIEW IF NOT EXISTS monitor.v_recent_anomalies AS
-- SELECT nename, host, metric_name, label, timestamp, actual_value, predicted_value, 
--        anomaly_score, direction, detected_at
-- FROM monitor.anomaly_detection_results
-- WHERE is_anomaly = 1 AND timestamp > now() - INTERVAL 1 HOUR
-- ORDER BY anomaly_score DESC;

-- 指标异常统计视图（24小时）
-- CREATE VIEW IF NOT EXISTS monitor.v_metric_anomaly_stats AS
-- SELECT nename, host, metric_name,
--     countIf(is_anomaly = 1) as anomaly_count_24h,
--     avgIf(anomaly_score, is_anomaly = 1) as avg_score_24h,
--     max(anomaly_score) as max_score_24h
-- FROM monitor.anomaly_detection_results
-- WHERE timestamp > now() - INTERVAL 24 HOUR
-- GROUP BY nename, host, metric_name
-- ORDER BY anomaly_count_24h DESC;

-- ============================================
-- 3. 实用查询示例
-- ============================================

-- 查询最近的严重异常（分数 > 2）
-- SELECT 
--     host,
--     metric_name,
--     label,
--     formatDateTime(timestamp, '%Y-%m-%d %H:%M:%S') as time,
--     round(actual_value, 2) as actual,
--     round(predicted_value, 2) as predicted,
--     round(anomaly_score, 2) as score,
--     CASE direction WHEN 1 THEN '↑' WHEN -1 THEN '↓' ELSE '=' END as trend
-- FROM monitor.anomaly_detection_results
-- WHERE is_anomaly = 1 AND anomaly_score > 2.0
--   AND timestamp > now() - INTERVAL 1 HOUR
-- ORDER BY anomaly_score DESC
-- LIMIT 20;

-- 查询特定主机的异常历史
-- SELECT timestamp, metric_name, actual_value, predicted_value, anomaly_score
-- FROM monitor.anomaly_detection_results
-- WHERE host LIKE '%as-1' AND timestamp > now() - INTERVAL 7 DAY
-- ORDER BY timestamp;

-- 异常趋势分析（按小时统计）
-- SELECT 
--     toStartOfHour(timestamp) as hour,
--     countIf(is_anomaly = 1) as anomaly_count,
--     avgIf(anomaly_score, is_anomaly = 1) as avg_score
-- FROM monitor.anomaly_detection_results
-- WHERE timestamp > now() - INTERVAL 24 HOUR
-- GROUP BY hour
-- ORDER BY hour;

-- 按指标类型统计异常分布
-- SELECT 
--     metric_name,
--     countIf(is_anomaly = 1) as anomaly_count,
--     max(anomaly_score) as max_score
-- FROM monitor.anomaly_detection_results
-- WHERE timestamp > now() - INTERVAL 24 HOUR
-- GROUP BY metric_name
-- ORDER BY anomaly_count DESC;

-- ============================================
-- 4. 表结构验证
-- ============================================
-- SELECT name, engine, partition_key, sorting_key
-- FROM system.tables 
-- WHERE database = 'monitor' AND name LIKE '%anomaly%'
-- ORDER BY name;