#!/bin/bash
# 异常检测结果快速查询脚本
# 使用: ./check_anomalies.sh [recent|severe|host|hour|summary]

CH_HOST="10.1.62.240"
CH_PORT="9002"
CH_PASS="1cmszx#YZSSY"

case "$1" in
    recent)
        # 最近异常
        clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
        SELECT 
            substring(host, 30) as host,
            metric_name,
            formatDateTime(timestamp, '%H:%M') as time,
            round(anomaly_score, 2) as score,
            round(actual_value, 2) as actual,
            round(predicted_value, 2) as predicted
        FROM monitor.anomaly_detection_results
        WHERE is_anomaly=1 AND timestamp > now() - INTERVAL 1 HOUR
        ORDER BY anomaly_score DESC
        LIMIT 20
        "
        ;;
    severe)
        # 严重异常
        clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
        SELECT * FROM monitor.anomaly_detection_results 
        WHERE anomaly_score > 2.0 
        ORDER BY anomaly_score DESC LIMIT 10
        "
        ;;
    host)
        # 按主机统计
        clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
        SELECT 
            substring(host, 30) as host,
            countIf(is_anomaly=1) as anomalies,
            max(anomaly_score) as max_score
        FROM monitor.anomaly_detection_results
        WHERE timestamp > now() - INTERVAL 24 HOUR
        GROUP BY host
        ORDER BY anomalies DESC
        "
        ;;
    hour)
        # 按小时统计
        clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
        SELECT 
            formatDateTime(toStartOfHour(timestamp), '%H:00') as hour,
            countIf(is_anomaly=1) as anomalies,
            count(*) as total
        FROM monitor.anomaly_detection_results
        WHERE timestamp > now() - INTERVAL 24 HOUR
        GROUP BY toStartOfHour(timestamp)
        ORDER BY toStartOfHour(timestamp)
        "
        ;;
    summary)
        # 综合摘要
        clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
        SELECT 
            count() as total_records,
            countIf(is_anomaly=1) as total_anomalies,
            countIf(anomaly_score > 2) as severe,
            countIf(anomaly_score > 1.5 AND anomaly_score <= 2) as moderate,
            uniqExact(host) as hosts_monitored,
            min(timestamp) as earliest,
            max(timestamp) as latest
        FROM monitor.anomaly_detection_results
        WHERE timestamp > now() - INTERVAL 24 HOUR
        "
        ;;
    *)
        echo "使用方法: $0 [recent|severe|host|hour|summary]"
        echo ""
        echo "  recent  - 最近1小时异常"
        echo "  severe  - 严重异常(分数>2)"
        echo "  host    - 按主机统计"
        echo "  hour    - 按小时统计"
        echo "  summary - 综合摘要"
        ;;
esac
