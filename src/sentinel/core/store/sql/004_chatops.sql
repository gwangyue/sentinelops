-- 飞书推送的游标（09 §4.2）：notifier 按 audit_events.seq 往后读，发送成功之后才推进（至少一次投递）。
-- 按环境分行（kind-dev / eks-prod）：两个 bot 各推各的，互不影响。
CREATE TABLE IF NOT EXISTS notify_cursor (
    env        text PRIMARY KEY,
    last_seq   bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- 按类型读新事件时用（推送只关心少数几种类型）
CREATE INDEX IF NOT EXISTS audit_events_type_seq ON audit_events (type, seq);
