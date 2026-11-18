USE evox_fleet;

CREATE TABLE IF NOT EXISTS robot_session (
    session_id VARCHAR(64) NOT NULL COMMENT '机器人连接会话ID',
    robot_id VARCHAR(128) NOT NULL COMMENT '机器狗业务ID',
    connected_at DATETIME(3) NOT NULL COMMENT '连接时间（UTC）',
    disconnected_at DATETIME(3) NULL COMMENT '断开时间（UTC）',
    status VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '会话状态',
    remote_info VARCHAR(255) NULL COMMENT '远端连接信息',
    edge_version VARCHAR(128) NULL COMMENT '边缘端版本',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (session_id),
    KEY idx_robot_session_robot_time (robot_id, connected_at),
    KEY idx_robot_session_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='机器狗连接会话';

CREATE TABLE IF NOT EXISTS robot_command (
    command_id VARCHAR(64) NOT NULL COMMENT '云端指令唯一ID',
    robot_id VARCHAR(128) NOT NULL COMMENT '机器狗业务ID',
    session_id VARCHAR(64) NULL COMMENT '机器人连接会话ID',
    browser_session VARCHAR(64) NULL COMMENT '发起指令的浏览器会话',
    request_id VARCHAR(128) NULL COMMENT '浏览器原始请求ID',
    upstream_id VARCHAR(128) NULL COMMENT '云端发送给机器人的请求ID',
    operation VARCHAR(64) NOT NULL COMMENT '指令操作',
    task_id VARCHAR(128) NULL COMMENT '关联巡检/导航任务',
    payload JSON NULL COMMENT '完整下发指令',
    status VARCHAR(32) NOT NULL DEFAULT 'issued' COMMENT '指令状态',
    issued_at DATETIME(3) NOT NULL COMMENT '云端受理时间（UTC）',
    sent_at DATETIME(3) NULL COMMENT '发送时间（UTC）',
    ack_at DATETIME(3) NULL COMMENT 'ACK时间（UTC）',
    ok TINYINT(1) NULL COMMENT 'ACK结果',
    message TEXT NULL COMMENT 'ACK消息',
    response JSON NULL COMMENT '完整ACK数据',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3),
    PRIMARY KEY (command_id),
    KEY idx_robot_command_robot_time (robot_id, issued_at),
    KEY idx_robot_command_status (status, updated_at),
    KEY idx_robot_command_session (session_id, issued_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='云端下发指令及ACK';

CREATE TABLE IF NOT EXISTS robot_data_object (
    object_id VARCHAR(64) NOT NULL COMMENT '数据对象ID',
    robot_id VARCHAR(128) NOT NULL COMMENT '机器狗业务ID',
    session_id VARCHAR(64) NULL COMMENT '机器人连接会话ID',
    direction VARCHAR(16) NOT NULL COMMENT 'up/down',
    stream_type VARCHAR(64) NOT NULL COMMENT '数据流类型',
    format VARCHAR(32) NOT NULL COMMENT '对象格式',
    period_start DATETIME(3) NOT NULL COMMENT '分片开始时间（UTC）',
    period_end DATETIME(3) NOT NULL COMMENT '分片结束时间（UTC）',
    message_count BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '消息数',
    byte_size BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '上传对象字节数',
    tos_bucket VARCHAR(128) NOT NULL COMMENT 'TOS Bucket',
    tos_key VARCHAR(512) NOT NULL COMMENT 'TOS对象Key',
    sha256 CHAR(64) NOT NULL COMMENT '上传对象SHA256',
    schema_version INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '数据协议版本',
    status VARCHAR(32) NOT NULL DEFAULT 'uploaded' COMMENT '归档状态',
    uploaded_at DATETIME(3) NOT NULL COMMENT '上传完成时间（UTC）',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    PRIMARY KEY (object_id),
    UNIQUE KEY uk_robot_data_object_tos_key (tos_bucket, tos_key),
    KEY idx_robot_data_object_robot_time (robot_id, period_start),
    KEY idx_robot_data_object_session (session_id, period_start),
    KEY idx_robot_data_object_type (stream_type, period_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='机器狗数据TOS对象清单';
