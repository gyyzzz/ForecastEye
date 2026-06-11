#!/usr/bin/env python3
"""
Prometheus + ClickHouse 时序异常检测脚本
使用 statsforecast 替代 Google 时序模型方案

适配表结构: monitor.metrics (10.1.62.240)
  - ts: DateTime (时间戳)
  - nename: String (网元名称: bjclas1bebm)
  - host: String (主机名: e20260403104438--vm-bjclas1bebm-cl-as-1 等)
  - metric_name: String (指标名: 28种)
  - metric_value: Float64 (指标值)
  - label: String (标签: 网卡名/磁盘名/进程名等)

数据特征:
  - 采样粒度: ~1分钟
  - 主机数: 9台 (as-1, as-2, mgr-1, mgr-2, db-1, db-2, db-3, eblb-1, eblb-2)
  - 指标类型: 28种（系统/网络/磁盘/进程/数据库/Redis/SDC）

依赖:
    pip install statsforecast clickhouse-connect pandas pyyaml

使用:
    python statsforecast_anomaly_detector.py --config config.yaml
    python statsforecast_anomaly_detector.py --config config.yaml --dry-run
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List

import clickhouse_connect
import numpy as np
import pandas as pd
import yaml
from statsforecast import StatsForecast
from statsforecast.models import (
    MSTL,
    AutoETS,
    AutoARIMA,
    SeasonalNaive,
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('anomaly_detector')

# unique_id 分隔符（避免主机名中下划线冲突）
UID_SEP = '#'


class AnomalyDetector:
    """基于 statsforecast 的时序异常检测器"""
    
    def __init__(self, config_path: str):
        """初始化检测器
        
        Args:
            config_path: YAML 配置文件路径
        """
        self.config = self._load_config(config_path)
        self.ch_client = self._init_clickhouse()
        self.sf_model = None  # 延迟初始化，根据实际数据粒度
        
    def _load_config(self, config_path: str) -> dict:
        """加载 YAML 配置"""
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        logger.info(f"配置加载成功: {config_path}")
        return config
    
    def _init_clickhouse(self):
        """初始化 ClickHouse 连接"""
        ch_config = self.config['clickhouse']
        client = clickhouse_connect.get_client(
            host=ch_config['host'],
            port=ch_config.get('port', 9000),
            username=ch_config.get('username', 'default'),
            password=ch_config.get('password', ''),
            database=ch_config.get('database', 'monitor')
        )
        logger.info(f"ClickHouse 连接成功: {ch_config['host']}:{ch_config.get('port', 9000)}, database={ch_config.get('database', 'monitor')}")
        return client
    
    def _init_model(self, freq: str = '1min') -> StatsForecast:
        """初始化 statsforecast 模型
        
        Args:
            freq: 数据粒度，根据实际数据确定
        """
        model_config = self.config.get('model', {})
        model_type = model_config.get('type', 'MSTL')
        
        # 根据数据粒度计算季节周期
        season_length = self._calc_season_length(freq)
        
        logger.info(f"模型初始化: {model_type}, freq={freq}, season_length={season_length}")
        
        # 选择模型
        if model_type == 'MSTL':
            models = [MSTL(season_length=season_length)]
        elif model_type == 'AutoETS':
            models = [AutoETS(season_length=season_length[0])]
        elif model_type == 'AutoARIMA':
            models = [AutoARIMA(season_length=season_length[0])]
        elif model_type == 'SeasonalNaive':
            models = [SeasonalNaive(season_length=season_length[0])]
        else:
            models = [MSTL(season_length=season_length)]
        
        sf = StatsForecast(
            models=models,
            freq=freq,
            n_jobs=model_config.get('n_jobs', 4),
        )
        return sf
    
    def _calc_season_length(self, freq: str) -> List[int]:
        """根据数据粒度计算季节周期
        
        运维指标典型周期:
        - 日周期: 业务访问规律（早高峰、晚高峰）
        - 周周期: 工作日/周末差异
        """
        freq_map = {
            '1min': [1440, 10080],      # 日=1440点(24h*60min), 周=10080点
            '5min': [288, 2016],        # 日=288点, 周=2016点
            '10min': [144, 1008],
            '15min': [96, 672],
            '30min': [48, 336],
            '1h': [24, 168],
            '1d': [7, 30],
        }
        return freq_map.get(freq, [1440, 10080])
    
    def _detect_freq(self, df: pd.DataFrame) -> str:
        """根据数据自动检测采样频率
        
        Args:
            df: 时序数据
            
        Returns:
            频率字符串如 '1min', '5min' 等
        """
        if df.empty:
            return '1min'
        
        # 取一个时序样本分析
        sample_uid = df['unique_id'].iloc[0]
        sample_df = df[df['unique_id'] == sample_uid].sort_values('ds')
        
        if len(sample_df) < 2:
            return '1min'
        
        # 计算时间间隔
        intervals = sample_df['ds'].diff().dropna()
        median_interval = intervals.median()
        
        # 映射到频率字符串
        if median_interval.total_seconds() <= 90:
            return '1min'
        elif median_interval.total_seconds() <= 360:
            return '5min'
        elif median_interval.total_seconds() <= 960:
            return '15min'
        elif median_interval.total_seconds() <= 1800:
            return '30min'
        else:
            return '1h'
    
    def fetch_metrics(self) -> pd.DataFrame:
        """从 monitor.metrics 查询时序数据
        
        unique_id 构建: 使用 '#' 分隔符避免主机名冲突
        格式: nename#host#metric_name#label
        
        Returns:
            符合 statsforecast 格式的 DataFrame
        """
        query_config = self.config['query']
        database = self.config['clickhouse'].get('database', 'monitor')
        table = query_config['table']
        
        # 构建查询 SQL
        # 使用 '#' 作为分隔符，避免主机名中的下划线冲突
        sql = f"""
        SELECT 
            concat(nename, '{UID_SEP}', host, '{UID_SEP}', metric_name, '{UID_SEP}', 
                   if(label = '', 'default', label)) as unique_id,
            toDateTime(ts) as ds,
            metric_value as y
        FROM {database}.{table}
        WHERE 
            ts >= now() - INTERVAL {query_config['lookback_hours']} HOUR
            AND ts < now()
            {query_config.get('filter', '')}
        ORDER BY unique_id, ds
        """
        
        logger.info(f"执行查询: lookback={query_config['lookback_hours']}h")
        result = self.ch_client.query(sql)
        
        # 转换为 DataFrame
        df = pd.DataFrame(
            result.result_rows,
            columns=['unique_id', 'ds', 'y']
        )
        
        # 确保数据类型正确
        df['ds'] = pd.to_datetime(df['ds'])
        df['y'] = df['y'].astype(float)
        
        # 过滤样本不足的时序
        min_samples = self.config.get('threshold', {}).get('min_samples', 100)
        uid_counts = df.groupby('unique_id').size()
        valid_uids = uid_counts[uid_counts >= min_samples].index
        df = df[df['unique_id'].isin(valid_uids)]
        
        logger.info(f"查询完成: {len(df)} 条记录, {df['unique_id'].nunique()} 个有效时序 (样本>={min_samples})")
        
        if df.empty:
            logger.warning("数据为空或样本不足")
        
        return df
    
    def detect_anomalies(self, df: pd.DataFrame) -> pd.DataFrame:
        """执行异常检测（增量模式）
        
        检测窗口：forecast_horizon 个点（用于预测计算）
        实际写入：只写后 write_increment 个点（新增区间，避免重复）
        
        Args:
            df: 符合 statsforecast 格式的时序数据
            
        Returns:
            包含异常分数的 DataFrame（只包含增量区间）
        """
        if df.empty:
            logger.warning("数据为空，跳过检测")
            return pd.DataFrame()
        
        # 自动检测数据频率
        freq = self._detect_freq(df)
        
        # 预测参数
        h = self.config.get('model', {}).get('forecast_horizon', 10)  # 检测窗口
        write_inc = self.config.get('model', {}).get('write_increment', 5)  # 写入增量
        level = self.config.get('model', {}).get('confidence_level', [95])
        
        # 确保写入增量不超过检测窗口
        if write_inc > h:
            write_inc = h
        
        logger.info(f"开始预测: 检测窗口={h}点, 写入增量={write_inc}点, freq={freq}")
        
        # 按时序分组处理
        results = []
        
        for uid in df['unique_id'].unique():
            uid_df = df[df['unique_id'] == uid].sort_values('ds')
            
            if len(uid_df) <= h:
                continue
            
            # 分割训练集和验证集
            train_df = uid_df.iloc[:-h].copy()
            test_df = uid_df.iloc[-h:].copy()  # 检测窗口：最后 h 个点
            
            if len(train_df) < 50:
                continue
            
            # DEBUG: 记录时序的时间范围
            test_start = test_df['ds'].min()
            test_end = test_df['ds'].max()
            
            # 初始化模型
            model_config = self.config.get('model', {})
            season_length = model_config.get('season_length', [1440])
            season_length = min(season_length[0], len(train_df) // 2)
            
            try:
                sf = StatsForecast(
                    models=[SeasonalNaive(season_length=season_length)],
                    freq=freq,
                    n_jobs=1,
                )
                
                forecast_df = sf.forecast(df=train_df, h=h, level=level)
                
                # 合并预测结果和实际值（完整检测窗口）
                merged = test_df.merge(forecast_df, on=['unique_id', 'ds'], how='inner')
                
                if merged.empty:
                    continue
                
                # 计算异常分数
                model_name = 'SeasonalNaive'
                predictions = merged[model_name].values
                actuals = merged['y'].values
                
                # 获取置信区间
                lo_col = f'{model_name}-lo-{level[0]}'
                hi_col = f'{model_name}-hi-{level[0]}'
                
                if lo_col in merged.columns and hi_col in merged.columns:
                    interval_width = merged[hi_col].values - merged[lo_col].values
                    interval_half = interval_width / 2 + 1e-6
                    deviation = np.abs(actuals - predictions)
                    anomaly_scores = deviation / interval_half
                else:
                    std_dev = train_df['y'].std() + 1e-6
                    anomaly_scores = np.abs(actuals - predictions) / std_dev
                
                direction = np.sign(actuals - predictions).astype(int)
                threshold = self.config.get('threshold', {}).get('score_threshold', 1.5)
                is_anomaly = anomaly_scores > threshold
                
                # ========== 增量写入：只取后 write_inc 个点 ==========
                # 检测窗口是 h 个点，但只写入后 write_inc 个点
                # 前面的 (h - write_inc) 个点在上次运行中已经写入过了
                
                # 注意：merged 可能少于 h 个点（如果预测失败部分）
                actual_merged_len = len(merged)
                start_idx = max(0, actual_merged_len - write_inc)  # 确保不越界
                
                logger.debug(f"时序 {uid}: merged={actual_merged_len}点, 写入起点={start_idx}, 时间范围={test_start}~{test_end}")
                
                for i in range(start_idx, actual_merged_len):
                    results.append({
                        'unique_id': merged['unique_id'].iloc[i],
                        'ds': merged['ds'].iloc[i],
                        'y': actuals[i],
                        'y_pred': predictions[i],
                        'anomaly_score': anomaly_scores[i],
                        'direction': direction[i],
                        'is_anomaly': is_anomaly[i],
                        'point_index': i - start_idx,  # 在增量区间内的位置（0=最早，write_inc-1=最新）
                    })
                    
            except Exception as e:
                logger.warning(f"时序 {uid} 预测失败: {e}")
                continue
        
        anomaly_df = pd.DataFrame(results)
        
        anomaly_count = anomaly_df['is_anomaly'].sum() if not anomaly_df.empty else 0
        total_points = len(anomaly_df) if not anomaly_df.empty else 0
        
        logger.info(f"检测完成: 检测窗口{h}点, 写入增量{write_inc}点 → 实际写入{total_points}条, {anomaly_count}条异常")
        
        return anomaly_df
    
    def _compute_anomaly_score(
        self,
        actual_df: pd.DataFrame,
        forecast_df: pd.DataFrame
    ) -> pd.DataFrame:
        """计算异常分数
        
        异常分数定义:
        - score = |实际值 - 预测值| / (置信区间半宽度)
        - score > 1 表示超出置信区间
        """
        # 获取预测时间范围
        forecast_start = forecast_df['ds'].min()
        forecast_end = forecast_df['ds'].max()
        
        # 过滤实际数据到预测时间段
        actual_recent = actual_df[
            (actual_df['ds'] >= forecast_start) & 
            (actual_df['ds'] <= forecast_end)
        ].copy()
        
        if actual_recent.empty:
            logger.warning(f"预测时间段内无实际数据: {forecast_start} - {forecast_end}")
            return pd.DataFrame()
        
        # 合并实际值和预测值
        merged = actual_recent.merge(
            forecast_df,
            on=['unique_id', 'ds'],
            how='inner'
        )
        
        if merged.empty:
            logger.warning("合并后数据为空（时间点不匹配）")
            return pd.DataFrame()
        
        # 获取模型列名
        model_cols = [c for c in forecast_df.columns 
                      if c not in ['unique_id', 'ds'] 
                      and '-lo-' not in c 
                      and '-hi-' not in c]
        
        if not model_cols:
            logger.warning("未找到预测模型结果")
            return pd.DataFrame()
        
        model_name = model_cols[0]
        
        predictions = merged[model_name].values
        actuals = merged['y'].values
        
        # 获取置信区间
        level = self.config.get('model', {}).get('confidence_level', [95])[0]
        lo_col = f'{model_name}-lo-{level}'
        hi_col = f'{model_name}-hi-{level}'
        
        if lo_col in merged.columns and hi_col in merged.columns:
            interval_width = merged[hi_col].values - merged[lo_col].values
            interval_half = interval_width / 2 + 1e-6
            
            deviation = np.abs(actuals - predictions)
            anomaly_scores = deviation / interval_half
            direction = np.sign(actuals - predictions).astype(int)
        else:
            # 无置信区间时使用标准差
            deviation = np.abs(actuals - predictions)
            std_dev = np.std(actuals) if len(actuals) > 1 else 1.0
            anomaly_scores = deviation / (std_dev + 1e-6)
            direction = np.sign(actuals - predictions).astype(int)
        
        # 构建结果 DataFrame
        result = pd.DataFrame({
            'unique_id': merged['unique_id'],
            'ds': merged['ds'],
            'y': actuals,
            'y_pred': predictions,
            'anomaly_score': anomaly_scores,
            'direction': direction,
            'is_anomaly': anomaly_scores > self.config.get('threshold', {}).get('score_threshold', 1.5),
        })
        
        # 统计异常数
        anomaly_count = result['is_anomaly'].sum()
        threshold = self.config.get('threshold', {}).get('score_threshold', 1.5)
        logger.info(f"检测到异常: {anomaly_count} 条 (阈值: {threshold})")
        
        return result
    
    def save_results(self, anomaly_df: pd.DataFrame):
        """保存异常检测结果到 ClickHouse
        
        增量写入模式：只写入新增区间，无需删除操作
        
        Args:
            anomaly_df: 异常检测结果（已过滤为增量区间）
        """
        if anomaly_df.empty:
            logger.info("无结果需要保存")
            return
        
        output_config = self.config.get('output', {})
        database = self.config['clickhouse'].get('database', 'monitor')
        table = output_config.get('table', 'anomaly_detection_results')
        
        # 解析 unique_id 获取原始字段（使用 '#' 分隔符）
        parts = anomaly_df['unique_id'].str.split(UID_SEP)
        anomaly_df['nename'] = parts.str[0]
        anomaly_df['host'] = parts.str[1]
        anomaly_df['metric_name'] = parts.str[2]
        anomaly_df['label'] = parts.str[3] if parts.str.len().max() > 3 else 'default'
        
        # 添加检测时间
        anomaly_df['detected_at'] = datetime.now()
        anomaly_df['model_name'] = self.config.get('model', {}).get('type', 'SeasonalNaive')
        
        # 时间范围（用于日志）
        min_ts = anomaly_df['ds'].min().strftime('%Y-%m-%d %H:%M')
        max_ts = anomaly_df['ds'].max().strftime('%Y-%m-%d %H:%M')
        
        # 转换为适合 ClickHouse 的格式
        insert_data = anomaly_df[
            ['nename', 'host', 'metric_name', 'label', 'ds', 'y', 'y_pred', 
             'anomaly_score', 'direction', 'is_anomaly', 'detected_at', 'model_name']
        ].values.tolist()
        
        columns = ['nename', 'host', 'metric_name', 'label', 'timestamp', 
                   'actual_value', 'predicted_value', 'anomaly_score', 
                   'direction', 'is_anomaly', 'detected_at', 'model_name']
        
        self.ch_client.insert(table, insert_data, column_names=columns)
        logger.info(f"结果已保存: {len(insert_data)} 条 -> {database}.{table} (时间范围: {min_ts} ~ {max_ts})")
    
    def run(self):
        """执行完整的异常检测流程"""
        logger.info("=" * 60)
        logger.info("开始异常检测流程")
        logger.info("=" * 60)
        
        try:
            # 1. 获取数据
            df = self.fetch_metrics()
            
            if df.empty:
                logger.warning("无数据，流程终止")
                return
            
            # 2. 异常检测
            anomaly_df = self.detect_anomalies(df)
            
            if anomaly_df.empty:
                logger.warning("无检测结果")
                return
            
            # 3. 保存结果
            self.save_results(anomaly_df)
            
            # 4. 输出摘要
            anomalies = anomaly_df[anomaly_df['is_anomaly']]
            if not anomalies.empty:
                logger.info("=" * 60)
                logger.info("异常摘要")
                logger.info("=" * 60)
                summary = anomalies.groupby('unique_id').agg({
                    'anomaly_score': 'max',
                    'ds': 'count'
                }).rename(columns={'ds': 'anomaly_count'})
                
                for idx, row in summary.head(20).iterrows():
                    # 解析 unique_id 显示
                    parts = idx.split(UID_SEP)
                    display = f"{parts[1][:20]}... | {parts[2]}" if len(parts) > 2 else idx
                    logger.info(f"  {display}: {row['anomaly_count']} 次异常, 最高分数 {row['anomaly_score']:.2f}")
            
            logger.info("=" * 60)
            logger.info("异常检测完成")
            logger.info("=" * 60)
            
        except Exception as e:
            logger.error(f"异常检测失败: {e}")
            raise


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='Prometheus + ClickHouse 时序异常检测 (适配 monitor.metrics)'
    )
    parser.add_argument(
        '--config', 
        default='config.yaml',
        help='配置文件路径 (默认: config.yaml)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='试运行模式: 只查询不写入'
    )
    
    args = parser.parse_args()
    
    if not Path(args.config).exists():
        logger.error(f"配置文件不存在: {args.config}")
        sys.exit(1)
    
    detector = AnomalyDetector(args.config)
    
    if args.dry_run:
        logger.info("DRY-RUN 模式: 只查询不写入")
        df = detector.fetch_metrics()
        if not df.empty:
            anomaly_df = detector.detect_anomalies(df)
            if not anomaly_df.empty:
                print("\n检测结果预览:")
                print(anomaly_df[anomaly_df['is_anomaly']].head(20).to_string())
    else:
        detector.run()


if __name__ == '__main__':
    main()