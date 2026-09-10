-- 迁移：content_analysis 新增 market_expectations 字段，单独存储"后市预期"数组
-- 将原嵌在 llm_summary JSON 里的后市预期提取到独立列，便于直接查询与分析
--
-- 用法:
--   mysql -h 127.0.0.1 -P 3306 -u root -p stock_blog < migrations/20260910_add_market_expectations.sql

-- 1. 新增列：JSON 类型，放在 mentioned_sectors 之后（同为从 summary 提取的冗余字段）
ALTER TABLE `content_analysis`
  ADD COLUMN `market_expectations` JSON DEFAULT NULL
    COMMENT '后市预期数组, 如[{"标的":"大盘","方向":"看涨","时间维度":"次日","具体描述":"..."}]'
  AFTER `mentioned_sectors`;

-- 2. 回填存量数据：从 llm_summary 提取"后市预期"节点写入新列
--    JSON_EXTRACT 路径 '$."后市预期"' 对应中文键名
UPDATE `content_analysis`
SET `market_expectations` = JSON_EXTRACT(`llm_summary`, '$."后市预期"')
WHERE `llm_summary` IS NOT NULL
  AND JSON_VALID(`llm_summary`) = 1
  AND JSON_LENGTH(JSON_EXTRACT(`llm_summary`, '$."后市预期"')) > 0;

-- 3. 验证：查看回填结果
SELECT content_id,
       JSON_LENGTH(`market_expectations`) AS expectation_count,
       JSON_PRETTY(`market_expectations`) AS expectations
FROM `content_analysis`
WHERE `market_expectations` IS NOT NULL
LIMIT 5;
