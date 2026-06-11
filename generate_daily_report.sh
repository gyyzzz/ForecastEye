#!/bin/bash
# 异常检测日报生成脚本
# 每天运行，生成过去24小时的异常检测报告

REPORT_DIR="/data/statsforecast_anomaly_detector/reports"
DATE=$(date +%Y%m%d)
REPORT_FILE="$REPORT_DIR/anomaly_report_$DATE.md"

mkdir -p $REPORT_DIR

CH_HOST="10.1.62.240"
CH_PORT="9002"
CH_PASS="1cmszx#YZSSY"

echo "# 异常检测日报 - $(date '+%Y-%m-%d')" > $REPORT_FILE
echo "" >> $REPORT_FILE
echo "报告时间范围: 过去24小时" >> $REPORT_FILE
echo "生成时间: $(date '+%Y-%m-%d %H:%M:%S')" >> $REPORT_FILE
echo "" >> $REPORT_FILE

echo "## 一、检测概况" >> $REPORT_FILE
echo "" >> $REPORT_FILE

# 获取统计数据
STATS=$(clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
SELECT 
    count() as total,
    countIf(is_anomaly=1) as anomalies,
    countIf(anomaly_score > 2) as severe,
    countIf(anomaly_score > 1.5 AND anomaly_score <= 2) as moderate,
    uniqExact(host) as hosts
FROM monitor.anomaly_detection_results
WHERE timestamp > now() - INTERVAL 24 HOUR
FORMAT TSVRaw
")

TOTAL=$(echo "$STATS" | cut -f1)
ANOMALIES=$(echo "$STATS" | cut -f2)
SEVERE=$(echo "$STATS" | cut -f3)
MODERATE=$(echo "$STATS" | cut -f4)
HOSTS=$(echo "$STATS" | cut -f5)

echo "| 指标 | 数值 |" >> $REPORT_FILE
echo "|------|------|" >> $REPORT_FILE
echo "| 总检测记录 | $TOTAL |" >> $REPORT_FILE
echo "| 异常记录 | $ANOMALIES |" >> $REPORT_FILE
echo "| 严重异常(>2) | $SEVERE |" >> $REPORT_FILE
echo "| 中度异常(1.5-2) | $MODERATE |" >> $REPORT_FILE
echo "| 监控主机数 | $HOSTS |" >> $REPORT_FILE

echo "" >> $REPORT_FILE
echo "## 二、各主机异常统计" >> $REPORT_FILE
echo "" >> $REPORT_FILE
clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
SELECT 
    substring(host, 30) as host,
    countIf(is_anomaly=1) as anomalies,
    round(max(anomaly_score), 2) as max_score
FROM monitor.anomaly_detection_results
WHERE timestamp > now() - INTERVAL 24 HOUR
GROUP BY host
ORDER BY anomalies DESC
FORMAT Markdown
" >> $REPORT_FILE

echo "" >> $REPORT_FILE
echo "## 三、异常时段分布" >> $REPORT_FILE
echo "" >> $REPORT_FILE
clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
SELECT 
    formatDateTime(toStartOfHour(timestamp), '%H:00') as hour,
    countIf(is_anomaly=1) as anomalies,
    round(avgIf(anomaly_score, is_anomaly=1), 2) as avg_score
FROM monitor.anomaly_detection_results
WHERE timestamp > now() - INTERVAL 24 HOUR
GROUP BY toStartOfHour(timestamp)
ORDER BY toStartOfHour(timestamp)
FORMAT Markdown
" >> $REPORT_FILE

echo "" >> $REPORT_FILE
echo "## 四、严重异常详情" >> $REPORT_FILE
echo "" >> $REPORT_FILE
clickhouse-client --host $CH_HOST --port $CH_PORT --password "$CH_PASS" -q "
SELECT 
    substring(host, 30) as host,
    metric_name,
    formatDateTime(timestamp, '%H:%M') as time,
    round(anomaly_score, 2) as score,
    round(actual_value, 2) as actual,
    round(predicted_value, 2) as predicted,
    CASE direction WHEN 1 THEN '高于' WHEN -1 THEN '低于' END as trend
FROM monitor.anomaly_detection_results
WHERE anomaly_score > 1.5 AND timestamp > now() - INTERVAL 24 HOUR
ORDER BY anomaly_score DESC
LIMIT 20
FORMAT Markdown
" >> $REPORT_FILE

echo "" >> $REPORT_FILE
echo "---" >> $REPORT_FILE
echo "*报告自动生成*" >> $REPORT_FILE

echo "日报已生成: $REPORT_FILE"
cat $REPORT_FILE