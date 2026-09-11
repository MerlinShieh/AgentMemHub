"""脱敏模块测试：命中各类凭据/个人信息，且**不误伤正常技术文本**。

误报会毁掉记忆价值，因此"不误报"用例与"命中"用例同等重要。
"""
from __future__ import annotations

from agentmemhub.sanitize import (
    PLACEHOLDER,
    Finding,
    has_substance,
    is_clean,
    redact,
    scan,
)

# 假凭据样本：**运行时拼接**，源码里不出现完整凭据格式——
# 否则 scripts/sensitive_scan.py（仓库推送前的兜底扫描）会把测试样本
# 误报为真实泄漏，导致扫描结果不可信。
AWS_KEY = "AKIA" + "X" * 16
GITHUB_PAT = "github_pat_" + "X" * 20
GH_TOKEN = "ghp_" + "X" * 24
OPENAI_KEY = "sk-" + "X" * 20
SLACK_TOKEN = "xoxb-" + "X" * 20
BEARER = "Bearer " + "X" * 30


# ── 凭据类：必须命中 ──────────────────────────────────────────────────

def test_openai_style_key_hits():
    f = scan(f"配置里写了 api key: {OPENAI_KEY}")
    assert any(x.pattern == "openai_key" for x in f)


def test_github_tokens_hit():
    assert not is_clean(f"token={GITHUB_PAT}")
    assert not is_clean(GH_TOKEN)


def test_private_key_block_hits():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEA1234567890abcdef\n"
           "-----END RSA PRIVATE KEY-----")
    f = scan(f"部署私钥如下：\n{pem}\n请勿外传")
    assert any(x.pattern == "private_key" for x in f)


def test_jwt_hits():
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
           "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    assert not is_clean(jwt)


def test_aws_and_bearer_and_slack_hits():
    assert not is_clean(AWS_KEY)
    assert not is_clean(f"Authorization: {BEARER}")
    assert not is_clean(SLACK_TOKEN)


def test_password_assignment_hits():
    f = scan("DATABASE_PASSWORD=SuperSecret123")
    assert any(x.pattern == "password_assign" for x in f)


# ── 个人信息类：必须命中 ───────────────────────────────────────────────

def test_email_mobile_id_hits():
    assert any(x.pattern == "email" for x in scan("联系 user@example.com 处理"))
    assert any(x.pattern == "cn_mobile" for x in scan("手机号 13812345678"))
    assert any(x.pattern == "cn_id" for x in scan("身份证 110101199003078515"))


def test_pii_can_be_disabled():
    """include_pii=False 时只扫凭据，个人信息放行（供特定场景放宽）。"""
    assert is_clean("联系 a@b.com", include_pii=False)
    assert not is_clean("联系 a@b.com", include_pii=True)


# ── 不误报：技术文本必须完好 ──────────────────────────────────────────

def test_technical_text_not_flagged():
    """正常技术结论不得被误脱敏（误报=记忆价值归零）。"""
    samples = [
        "bge-small-zh-v1.5 维度 512，batch_size=32，按长度分桶后 26 条/秒",
        "采集库路径 database/agentmemhub.db，索引库 session_rag.db",
        "RRF 融合 k=60，权重 0.4，阈值 floor 0.2，去重阈值 0.92",
        "commit 452fcca 有 265 个测试，1 个跳过",
        "python -m agentmemhub sync 会增量向量化，耗时约 572 秒",
        "用户决定记忆引擎采用 RRF 融合向量与 trigram 两路召回",
        "SQL: SELECT source, conversation_id FROM units WHERE seq > ?",
        "端口 8086 被占用，PID 35252；引擎无独立守护进程",
        "unix_ms=1788965975000 时戳；unix_s=1788965975",
        "models/bge-base-zh-v1.5/onnx/model_quantized.onnx 大小 99MB",
    ]
    for s in samples:
        hits = scan(s)
        assert hits == [], f"误报 {[h.as_dict() for h in hits]} ← {s!r}"


def test_plain_numbers_not_mobile():
    """10 位时间戳、13 位毫秒戳、版本号等不得被判为手机号。"""
    for s in ("1788965975", "1788965975000", "512", "20260911"):
        assert is_clean(s), s


# ── 剥离行为 ──────────────────────────────────────────────────────────

def test_redact_replaces_and_records():
    text = f"我的 key 是 {OPENAI_KEY}，请记下"
    out, findings = redact(text)
    assert OPENAI_KEY not in out
    assert PLACEHOLDER in out
    assert len(findings) == 1
    assert findings[0].kind == "secret"
    assert isinstance(findings[0], Finding)


def test_redact_snippet_does_not_leak_full_secret():
    """命中记录只留摘要，避免日志本身成为泄漏源。"""
    secret = "sk-" + "X" * 24
    out, findings = redact(f"key={secret}")
    assert secret not in findings[0].snippet
    assert "…" in findings[0].snippet


def test_redact_keeps_technical_context():
    text = "用户决定用 RRF 融合；他的邮箱 a@b.com 仅用于示例"
    out, findings = redact(text)
    assert "RRF" in out and "融合" in out          # 技术内容保留
    assert "a@b.com" not in out                    # 个人信息剥离
    assert [f.pattern for f in findings] == ["email"]


def test_has_substance_detects_empty_shell():
    """剥空了的记忆应被判定为无实质内容（调用方据此丢弃整条）。"""
    assert has_substance("用户决定采用 RRF 融合方案")
    assert not has_substance(PLACEHOLDER)
    assert not has_substance(f"{PLACEHOLDER}{PLACEHOLDER}")
    assert not has_substance("   ")
    assert not has_substance("")


def test_has_substance_accepts_short_chinese_conclusion():
    """中文信息密度高：4 个有效字符即可独立成条（阈值曾误设 8 导致误丢）。"""
    assert has_substance("采用 RRF 融合")
    assert has_substance("维度钉死 384")
    # 英文短回应仍视为无实质
    assert not has_substance("ok")
    assert not has_substance(PLACEHOLDER + "ok")


def test_empty_input_safe():
    assert scan("") == [] and scan(None) == []
    assert redact("") == ("", [])
    assert redact(None) == ("", [])
