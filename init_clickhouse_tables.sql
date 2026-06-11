-- Prometheus + ClickHouse 时序异常检测表结构初始化
-- 
-- 使用说明:
--   clickhouse-client --host --port  --password 'xxx' < init_clickhouse_tables.sql
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

