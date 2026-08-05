-- 调用审计表：全量 tools/call（成功+失败），dashboard 聚合与明细面板的数据源
CREATE TABLE IF NOT EXISTS calls (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  time DATETIME(3) NOT NULL,
  server VARCHAR(64) NOT NULL,
  tool VARCHAR(128) NOT NULL,
  op VARCHAR(8) NOT NULL DEFAULT 'read',
  token_name VARCHAR(128) NOT NULL DEFAULT '',
  latency_ms INT NOT NULL DEFAULT 0,
  status VARCHAR(8) NOT NULL,
  error_type VARCHAR(32) NOT NULL DEFAULT '',
  trace VARCHAR(64) NOT NULL DEFAULT '',
  -- 失败面板数据源统一到此表：message 存错误信息，journey 存请求轨迹 JSON
  -- NOTE: MySQL 8 的 TEXT 列不支持字面量默认值（error 1101），必须用表达式默认值 DEFAULT ('...')
  message TEXT NOT NULL DEFAULT (''),
  journey TEXT NOT NULL DEFAULT ('[]'),
  INDEX idx_time (time),
  INDEX idx_server (server),
  INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
