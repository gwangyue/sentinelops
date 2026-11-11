-- 修复方案、审批、执行（03 §4.3–4.4；02 §3.4）。调查引擎和执行器共用这些表，读写都走 governance/remediation_store.py。
-- 执行器只信任这里的记录（ADR-005）：它拿到的只有 (plan_id, approval_id, idempotency_key)，其余自己来读。
CREATE TABLE IF NOT EXISTS plans (
    id               text PRIMARY KEY,
    investigation_id text NOT NULL,
    plan_hash        text NOT NULL,
    body             jsonb NOT NULL,            -- RemediationPlan
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS plans_inv ON plans (investigation_id);

CREATE TABLE IF NOT EXISTS approval_requests (
    id           text PRIMARY KEY,
    plan_id      text NOT NULL REFERENCES plans (id),
    plan_hash    text NOT NULL,
    requested_at timestamptz NOT NULL,
    expires_at   timestamptz NOT NULL
);

-- 一个审批请求只能有一个决定：重复批准 / 先拒后批都被主键挡住
CREATE TABLE IF NOT EXISTS approval_decisions (
    approval_id text PRIMARY KEY REFERENCES approval_requests (id),
    body        jsonb NOT NULL                  -- ApprovalDecision
);

-- 调查引擎 → 执行器的请求队列：执行器轮询，用 FOR UPDATE SKIP LOCKED 认领
CREATE TABLE IF NOT EXISTS execution_requests (
    id              bigserial PRIMARY KEY,
    kind            text NOT NULL CHECK (kind IN ('execute', 'rollback')),
    plan_id         text NOT NULL,
    approval_id     text NOT NULL,
    idempotency_key text NOT NULL,
    claimed_at      timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- 幂等：同一个键只能有一条执行结果（02 §3.4"执行是幂等的"）
CREATE TABLE IF NOT EXISTS executions (
    idempotency_key text PRIMARY KEY,
    body            jsonb NOT NULL,             -- ExecutionResult
    created_at      timestamptz NOT NULL DEFAULT now()
);
