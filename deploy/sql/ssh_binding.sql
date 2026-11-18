CREATE DATABASE IF NOT EXISTS evox_fleet
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

USE evox_fleet;

CREATE TABLE IF NOT EXISTS robot_device (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    device_id VARCHAR(128) NOT NULL COMMENT '机器狗业务设备ID',
    device_name VARCHAR(255) NOT NULL COMMENT '设备名称',
    device_model VARCHAR(128) NOT NULL COMMENT '设备型号',
    device_type VARCHAR(32) NOT NULL DEFAULT 'quadruped' COMMENT '设备类型',
    serial_number VARCHAR(128) NULL COMMENT '硬件序列号',
    firmware_version VARCHAR(128) NULL COMMENT '固件版本',
    device_enabled TINYINT(1) NOT NULL DEFAULT 1 COMMENT '设备是否启用',
    online TINYINT(1) NOT NULL DEFAULT 0 COMMENT '设备是否在线',
    status VARCHAR(32) NOT NULL DEFAULT 'unknown' COMMENT '设备状态',
    last_seen_at DATETIME(3) NULL COMMENT '最近在线时间（UTC）',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '更新时间（UTC）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_robot_device_id (device_id),
    UNIQUE KEY uk_robot_device_serial (serial_number),
    KEY idx_robot_device_status (online, status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='机器狗设备基础信息';

CREATE TABLE IF NOT EXISTS ssh_binding (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
    device_id VARCHAR(128) NOT NULL COMMENT '机器狗业务设备ID',
    host VARCHAR(255) NOT NULL COMMENT 'SSH 反向网关主机',
    port INT UNSIGNED NULL COMMENT 'SSH 反向网关端口',
    username VARCHAR(128) NOT NULL COMMENT 'SSH 用户名',
    status VARCHAR(32) NOT NULL DEFAULT 'unknown' COMMENT '绑定状态',
    binding_version VARCHAR(64) NOT NULL DEFAULT '0' COMMENT '绑定版本',
    last_seen_at DATETIME(3) NULL COMMENT '最近绑定活跃时间（UTC）',
    created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '创建时间（UTC）',
    updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
        ON UPDATE CURRENT_TIMESTAMP(3) COMMENT '更新时间（UTC）',
    PRIMARY KEY (id),
    UNIQUE KEY uk_ssh_binding_device (device_id),
    UNIQUE KEY uk_ssh_binding_port (port),
    KEY idx_ssh_binding_status (status, updated_at),
    CONSTRAINT fk_ssh_binding_device
        FOREIGN KEY (device_id) REFERENCES robot_device(device_id)
        ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
  COMMENT='机器狗 SSH 端口绑定';
