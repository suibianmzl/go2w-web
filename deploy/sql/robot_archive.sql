USE evox_fleet;

CREATE TABLE IF NOT EXISTS robot_session (
    session_id VARCHAR(64) NOT NULL COMMENT '机器人连接会话ID',
    robot_id VARCHAR(128) NOT NULL COMMENT '机器狗业务ID',
    connected_at DATETIME(3) NOT NULL COMMENT '连接时间（UTC）',
    disconnected_at DATETIME(3) NULL COMMENT '断开时间（UTC）',
    status VARCHAR(32) NOT NULL DEFAULT 'active' COMMENT '会话状态',
    remote_info VARCHAR(255) NULL COMMENT '远端连接信息',
    edge_version VARCHAR(128) NULL COMMENT '边缘端版本',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '记录创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '记录更新时间（UTC）',
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
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '记录创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '记录更新时间（UTC）',
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
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '记录创建时间（UTC）',
    PRIMARY KEY (object_id),
    UNIQUE KEY uk_robot_data_object_tos_key (tos_bucket, tos_key),
    KEY idx_robot_data_object_robot_time (robot_id, period_start),
    KEY idx_robot_data_object_session (session_id, period_start),
    KEY idx_robot_data_object_type (stream_type, period_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='机器狗数据TOS对象清单';

CREATE TABLE IF NOT EXISTS robot_incident (
    robot_id VARCHAR(128) NOT NULL COMMENT '机器狗业务ID',
    incident_id VARCHAR(128) NOT NULL COMMENT '异常唯一ID',
    started_at_ms BIGINT NOT NULL COMMENT '发生时间Unix毫秒',
    ended_at_ms BIGINT NULL COMMENT '结束时间Unix毫秒',
    duration_s DOUBLE NULL COMMENT '持续秒数',
    ongoing TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否持续中',
    incident_type VARCHAR(64) NOT NULL COMMENT '异常类型代码',
    label VARCHAR(128) NOT NULL COMMENT '异常类型名称',
    level VARCHAR(16) NOT NULL COMMENT 'warn/error',
    message TEXT NULL COMMENT '异常说明',
    map_name VARCHAR(255) NULL COMMENT '发生地图',
    payload JSON NOT NULL COMMENT '完整异常记录',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '记录创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '记录更新时间（UTC）',
    PRIMARY KEY (robot_id, incident_id),
    KEY idx_robot_incident_time (robot_id, started_at_ms),
    KEY idx_robot_incident_type (robot_id, incident_type, started_at_ms),
    KEY idx_robot_incident_ongoing (robot_id, ongoing, started_at_ms)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='机器狗异常归集记录';

CREATE TABLE IF NOT EXISTS robot_rtc_recording (
    recording_id CHAR(32) NOT NULL COMMENT '云端内部录制记录ID',
    robot_id VARCHAR(128) NOT NULL COMMENT '机器狗业务ID',
    online_session_id VARCHAR(64) NOT NULL COMMENT '机器狗本次在线会话ID',
    control_session_id VARCHAR(64) NULL COMMENT '创建时的控制连接会话ID',
    video_session_id VARCHAR(64) NULL COMMENT '创建时的视频连接会话ID',
    app_id VARCHAR(64) NOT NULL COMMENT '火山RTC应用ID',
    room_id VARCHAR(128) NOT NULL COMMENT 'RTC房间ID',
    task_id VARCHAR(128) NOT NULL COMMENT '火山RTC录制任务ID',
    publisher_user_id VARCHAR(128) NOT NULL COMMENT 'RTC视频发布者用户ID',
    business_id VARCHAR(128) NULL COMMENT 'RTC云端录制业务标识',
    start_reason VARCHAR(128) NULL COMMENT '发起录制原因',
    stop_reason VARCHAR(128) NULL COMMENT '停止录制原因',
    status VARCHAR(32) NOT NULL COMMENT '录制任务状态',
    requested_at DATETIME(3) NOT NULL COMMENT '请求录制时间（UTC）',
    recording_started_at DATETIME(3) NULL COMMENT '录制实际开始时间（UTC）',
    stopping_at DATETIME(3) NULL COMMENT '请求停止时间（UTC）',
    recording_stopped_at DATETIME(3) NULL COMMENT '录制实际结束时间（UTC）',
    upload_finished_at DATETIME(3) NULL COMMENT '文件上传完成时间（UTC）',
    duration_ms BIGINT UNSIGNED NULL COMMENT '录制总时长（毫秒）',
    file_count INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '生成文件数量',
    total_byte_size BIGINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '生成文件总字节数',
    tos_bucket VARCHAR(128) NULL COMMENT 'TOS存储桶',
    request_config JSON NULL COMMENT 'StartRecord请求配置（已脱敏）',
    error_code VARCHAR(128) NULL COMMENT '最近错误码',
    error_message TEXT NULL COMMENT '最近错误信息',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '记录创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '记录更新时间（UTC）',
    PRIMARY KEY (recording_id),
    UNIQUE KEY uk_rtc_recording_task (app_id, room_id, task_id),
    KEY idx_rtc_recording_session (online_session_id, requested_at),
    KEY idx_rtc_recording_robot_time (robot_id, requested_at),
    KEY idx_rtc_recording_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='RTC云端录制任务';

CREATE TABLE IF NOT EXISTS robot_rtc_recording_file (
    recording_file_id CHAR(32) NOT NULL COMMENT '录制文件记录ID',
    recording_id CHAR(32) NOT NULL COMMENT '关联的录制任务记录ID',
    file_index INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '同一任务内文件序号',
    file_format VARCHAR(16) NOT NULL COMMENT '文件格式',
    tos_bucket VARCHAR(128) NOT NULL COMMENT 'TOS存储桶',
    tos_key VARCHAR(512) NOT NULL COMMENT 'TOS对象Key',
    duration_ms BIGINT UNSIGNED NULL COMMENT '文件时长（毫秒）',
    byte_size BIGINT UNSIGNED NULL COMMENT '文件大小（字节）',
    started_at DATETIME(3) NULL COMMENT '文件开始时间（UTC）',
    ended_at DATETIME(3) NULL COMMENT '文件结束时间（UTC）',
    video_width INT UNSIGNED NULL COMMENT '视频宽度（像素）',
    video_height INT UNSIGNED NULL COMMENT '视频高度（像素）',
    video_codec VARCHAR(32) NULL COMMENT '视频编码',
    audio_codec VARCHAR(32) NULL COMMENT '音频编码',
    status VARCHAR(32) NOT NULL DEFAULT 'uploaded' COMMENT '文件状态',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '记录创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '记录更新时间（UTC）',
    PRIMARY KEY (recording_file_id),
    UNIQUE KEY uk_rtc_recording_file_object (tos_bucket, tos_key),
    KEY idx_rtc_recording_file_task (recording_id, file_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='RTC云端录制文件清单';
