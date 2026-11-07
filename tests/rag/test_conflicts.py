"""记忆冲突检测（规则层）测试：槽位抽取 / 变更词 / 分类 / 扫描 / 处置。

设计见 `docs/memory-lifecycle.md` §4。核心主张：**不用 LLM 也能抓住
个人/团队知识库里绝大多数"矛盾"**——它们多是版本演进而非语义对立。
"""
from __future__ import annotations

import sqlite3

import pytest

from agentmemhub import conflicts as cf
from agentmemhub.rag.ingest import ensure_vec_table, open_index


# ── L2 槽位抽取 ────────────────────────────────────────────────────────

def test_slots_extracts_structured_values():
    s = cf.slots("默认模型改为 xiaomi/mimo-v2.6-flash，端口 9100，半衰期 180 天")
    assert "xiaomi/mimo-v2.6-flash" in s["model"]
    assert "9100" in s["port"]
    assert s["num:天"] == {"180"}          # 数字按单位分桶


def test_slots_version_and_path():
    s = cf.slots("库在 D:\\data\\vibeCoding\\Agent_MemHub 的 AgentMemHub v3.0.0 版本上")
    # 版本号绑定主体名（裸版本号不构成槽位，否则不同主题的邻居会互相误判）
    assert s["ver:agentmemhub"] == {"3.0.0"}
    # 槽位值统一小写（路径比较大小写不敏感）
    assert any("vibecoding" in p for p in s["path"])


def test_bare_version_is_not_a_slot():
    """裸版本号不算槽位——实测 13 对里 12 对误报都是它造成的。"""
    assert "ver:" not in "".join(cf.slots("v2.0.0 Release 已创建").keys())
    # 带主体名才算
    s = cf.slots("Mobile_App_AutoTest v1.4.0 调整了目录结构")
    assert s["ver:mobile_app_autotest"] == {"1.4.0"}


def test_slots_ignores_bare_numbers():
    """裸数字（没有单位/端口词/版本号形态）不构成槽位——避免误报。"""
    assert cf.slots("一共 3 和 12 这些数") == {}
    # 带量词才算槽位（「3 个」「12 条」是可比的数量表述）
    assert cf.slots("一共 3 个")["num:个"]


def test_slots_num_unit_bucketed_by_unit():
    """数字槽位**按单位分桶**：不同量纲不可比（实测 1633 对误报的根因）。"""
    s = cf.slots("3 分钟完成，共 4897 条记忆")
    assert s["num:分钟"] == {"3"}
    assert s["num:条"] == {"4897"}
    # 不同单位不会被塞进同一个集合
    assert "num_unit" not in s


def test_classify_ignores_multi_value_slots():
    """长文本罗列多个数字/路径 → 不可比，不判冲突（宁漏报不误报）。"""
    v = cf.classify("并发 11.6、26、3、51 都是观察值",
                    "约 4897 条记录、1 个库")
    assert v is None, "多值槽位不可比，不能判成同槽位冲突"


def test_classify_skips_identical_text():
    """正文完全一致时调用方跳过（这里验判定层：同样文本无槽位差异 → None）。"""
    t = "端口 8086 的服务配置说明"
    assert cf.classify(t, t) is None


def test_dual_projection_not_flagged():
    """`mcp_<h>` 与 `dst_<h>` 是同一内容双投影，不是冲突。"""
    assert cf._dual_projection("mcp_abc123", "dst_abc123")
    assert cf._dual_projection("dst_abc123", "mcp_abc123")
    assert not cf._dual_projection("mcp_abc123", "mcp_def456"), "不同内容不算"
    assert not cf._dual_projection("dst_abc123", "dst_abc123"), "同前缀不算"
    assert not cf._dual_projection(None, "dst_abc")


# ── L1 变更词 ──────────────────────────────────────────────────────────

def test_change_markers_hit():
    assert cf.change_markers_hit("默认模型改为 mimo-v2.6") == ["改为"]
    assert cf.change_markers_hit("v2.5 已废弃") == ["废弃"]
    assert cf.change_markers_hit("This is deprecated now") == ["deprecated"]
    assert cf.change_markers_hit("正常的一段技术结论，没有变更") == []


# ── 分类 ───────────────────────────────────────────────────────────────

def test_classify_conflict_by_slot():
    """同槽位不同值 → conflict（硬证据，优于变更词）。"""
    v = cf.classify("端口改为 9100", "服务端口 8086")
    assert v is not None and v[0] == "conflict"
    assert "port" in v[1]


def test_classify_param_change_by_shared_noun():
    """参数名词 + 变更词 + 数值不同 → conflict（知识库最有价值的一类）。

    "半衰期 30 天" vs "半衰期改为 180 天"：两条都在说同一个**参数**，
    且新方明确是变更句——这才是"同一属性的两个候选值"。
    """
    v = cf.classify("半衰期改为 180 天", "半衰期 30 天")
    assert v is not None and v[0] == "conflict"
    assert "半衰期" in v[1] and "180" in v[1]
    assert v[2]["param_nouns"] == ["半衰期"]


def test_classify_number_diff_without_param_noun_is_ignored():
    """没有共享参数名词的数量差异不判（「M1 完成 106 条」vs「M3 完成 5 条」）。"""
    assert cf.classify("M3 完成，全部 5 条记忆", "M1 完成，全部 106 条记忆") is None


def test_classify_number_diff_without_marker_is_ignored():
    """有参数名词但没有变更词 → 不判（可能是两处不同描述而非改写）。"""
    assert cf.classify("超时设为 900 秒", "超时 60 秒") is None


def test_classify_same_series_model_upgrade():
    """同一系列模型换代 → evolution；跨系列不判。"""
    v = cf.classify("默认模型已换成 xiaomi/mimo-v2.6-flash",
                    "默认模型使用 xiaomi/mimo-v2.5")
    assert v is not None and v[0] == "evolution"
    assert "同系列" in v[1]
    # 跨系列：各自不同主题，不算换代
    assert cf.classify("NVIDIA NIM 接入 z-ai/glm-5.2",
                       "阿里套餐支持 qwen3.7/3.6") is None


def test_classify_evolution_by_version():
    """同一主体的版本换代 → evolution（精度最高的一类信号）。"""
    v = cf.classify("Mobile_App_AutoTest v1.4.0 调整了目录结构",
                    "Mobile_App_AutoTest v1.1.0 删除了若干目录")
    assert v is not None and v[0] == "evolution"
    assert "ver:mobile_app_autotest" in v[1]


def test_classify_model_diff_does_not_trigger():
    """模型不同不触发——实测 6 对全是"推荐哪个模型" vs "某通道用哪个" 的误报。"""
    assert cf.classify("推荐用 xiaomi/mimo-v2.6-flash 做编译",
                       "NVIDIA NIM 接入 z-ai/glm-5.2") is None


def test_change_marker_alone_does_not_trigger():
    """纪律 1：变更词**不单独触发**——技术记忆几乎每条都在描述变更。

    实测教训：靠变更词独立判定，一次扫描产出 1877 对几乎全是噪音。
    它只作为 detail 里的证据供人工判读。
    """
    v = cf.classify("这套方案已废弃，不再维护", "这套方案是当前做法")
    assert v is None, "只有变更词、无槽位差异时不应报告"
    # 但证据仍要收进 detail 供人工判断——需要一条槽位差异把记录带出来
    v2 = cf.classify("端口改为 9100，旧的方案已废弃", "服务端口 8086")
    assert v2 is not None and v2[0] == "conflict"
    assert "废弃" in v2[2].get("markers", [])


def test_numeric_diff_alone_does_not_trigger():
    """纪律 2：数字差异（"72.7 秒" vs "89 秒"）不是矛盾，只作证据。"""
    assert cf.classify("首次全量编译耗时 72.7 秒", "另一次编译耗时 89 秒") is None


def test_path_diff_alone_does_not_trigger():
    """纪律 2：路径不同不代表冲突（不同场景/不同文件的路径天然不同）。"""
    assert cf.classify("产物在 /out/l1 目录", "配置在 /etc/app.yaml") is None


def test_classify_no_conflict_for_plain_text():
    assert cf.classify("测试覆盖率 82%", "代码风格用 ruff") is None


def test_classify_same_slot_same_value_is_not_conflict():
    """同一端口写了两遍（值相同）不能算冲突。"""
    assert cf.classify("端口 8086 可用", "服务监听端口 8086") is None


# ── 扫描与处置（需要向量表） ───────────────────────────────────────────

def _mk_env(tmp_path, project_settings, embedder):
    import dataclasses
    from agentmemhub.rag.ingest import ensure_vec_table, open_index

    active = project_settings.active_spec
    st = dataclasses.replace(project_settings,
                            index_db=tmp_path / "idx_conflicts.db")
    conn = open_index(st.index_db)
    ensure_vec_table(conn, active)
    return st, conn


def _add_unit(conn, active, embedder, text, *, ts, src_id):
    v = embedder.encode_passages([text])[0]
    with conn:
        # seq 用 ts：units 有 UNIQUE(source, conversation_id, seq)，写死会撞
        cur = conn.execute(
            "INSERT INTO units(source, conversation_id, seq, role, turn_key,"
            " src_id, time, title, text, chars)"
            " VALUES('memory','conv',?,'user','tk',?,?,'标题',?,?)",
            (ts, src_id, ts, text, len(text)))
        conn.execute(f"INSERT INTO {active.vec_table}(rowid, embedding)"
                     f" VALUES(?,?)", (cur.lastrowid, bytes(v)))
    return cur.lastrowid


def test_detect_finds_supersession(tmp_path, project_settings, embedder):
    """端到端：新结论取代旧结论 → 检出 evolution/conflict，方向正确。"""
    st, conn = _mk_env(tmp_path, project_settings, embedder)
    active = st.active_spec
    try:
        old_id = _add_unit(conn, active, embedder,
                           "默认模型使用 mimo-v2.5，端口 8086",
                           ts=1_000_000, src_id="mcp_old")
        new_id = _add_unit(conn, active, embedder,
                           "默认模型改为 mimo-v2.6，端口改为 9100",
                           ts=2_000_000, src_id="mcp_new")
        found = cf.detect_for_unit(conn, new_id, vec_table=active.vec_table,
                                   min_similarity=0.5)
        assert found, "应检出至少一条取代关系"
        rec = found[0]
        assert rec["unit_new"] == new_id and rec["unit_old"] == old_id, \
            "方向必须是『新的取代旧的』"
        assert rec["kind"] in ("conflict", "evolution")
    finally:
        conn.close()


def test_detect_is_idempotent(tmp_path, project_settings, embedder):
    """同一对不重复插（UNIQUE pair 索引）。"""
    st, conn = _mk_env(tmp_path, project_settings, embedder)
    active = st.active_spec
    try:
        _add_unit(conn, active, embedder, "端口 8086 的工作方式",
                  ts=1_000_000, src_id="mcp_a")
        nid = _add_unit(conn, active, embedder, "端口改为 9100 的工作方式",
                        ts=2_000_000, src_id="mcp_b")
        first = cf.detect_for_unit(conn, nid, vec_table=active.vec_table,
                                   min_similarity=0.3)
        second = cf.detect_for_unit(conn, nid, vec_table=active.vec_table,
                                    min_similarity=0.3)
        assert len(first) >= 1
        assert second == [], "重复扫描不应产生新记录"
        assert cf.stats(conn)["total"] >= 1
    finally:
        conn.close()


def test_resolve_superseded_archives_old(tmp_path, project_settings, embedder):
    """superseded：旧方被 manual_value=0 归档（撤出召回面），可清除恢复。"""
    from agentmemhub.rag.memstore import set_manual_value

    st, conn = _mk_env(tmp_path, project_settings, embedder)
    active = st.active_spec
    try:
        old_id = _add_unit(conn, active, embedder, "端口 8086 的配置",
                           ts=1_000_000, src_id="mcp_old2")
        new_id = _add_unit(conn, active, embedder, "端口改为 9100 的配置",
                           ts=2_000_000, src_id="mcp_new2")
        cf.detect_for_unit(conn, new_id, vec_table=active.vec_table,
                           min_similarity=0.3)
        rec = cf.list_conflicts(conn, status="open")[0]
        out = cf.resolve(conn, rec["id"], "superseded")
        assert out["changed"] and out["old_archived"]
        val = conn.execute("SELECT manual_value FROM unit_values WHERE unit_id=?",
                           (old_id,)).fetchone()
        assert val and val[0] == 0.0, "旧方 manual_value 锁 0 → 召回时被剔除"
        # 可逆：清除加权即恢复
        set_manual_value(conn, old_id, None)
        assert conn.execute("SELECT manual_value FROM unit_values WHERE unit_id=?",
                            (old_id,)).fetchone()[0] is None
        # 已处置的不再出现在 open 列表
        assert rec["id"] not in [r["id"] for r in cf.list_conflicts(conn)]
    finally:
        conn.close()


def test_resolve_refuses_to_override_user_weight(tmp_path, project_settings,
                                                 embedder):
    """旧方已有手动加权时拒绝自动归档——用户意志优先。"""
    from agentmemhub.rag.memstore import set_manual_value

    st, conn = _mk_env(tmp_path, project_settings, embedder)
    active = st.active_spec
    try:
        old_id = _add_unit(conn, active, embedder, "端口 8086 的配置说明",
                           ts=1_000_000, src_id="mcp_old3")
        new_id = _add_unit(conn, active, embedder, "端口改为 9100 的配置说明",
                          ts=2_000_000, src_id="mcp_new3")
        set_manual_value(conn, old_id, 0.9)      # 用户 ⭐ 锁过
        cf.detect_for_unit(conn, new_id, vec_table=active.vec_table,
                           min_similarity=0.3)
        rec = cf.list_conflicts(conn, status="open")[0]
        out = cf.resolve(conn, rec["id"], "superseded")
        assert not out["changed"] and "手动加权" in out["note"]
        assert conn.execute(
            "SELECT manual_value FROM unit_values WHERE unit_id=?",
            (old_id,)).fetchone()[0] == 0.9, "用户锁定的值不得被覆盖"
    finally:
        conn.close()


def test_scan_counts(tmp_path, project_settings, embedder):
    st, conn = _mk_env(tmp_path, project_settings, embedder)
    active = st.active_spec
    try:
        _add_unit(conn, active, embedder, "端口 8086 的服务配置",
                  ts=1_000_000, src_id="mcp_s1")
        _add_unit(conn, active, embedder, "端口改为 9100 的服务配置",
                  ts=2_000_000, src_id="mcp_s2")
        st_out = cf.scan(conn, vec_table=active.vec_table, min_similarity=0.3)
        assert st_out["scanned"] == 2
        assert st_out["errors"] == 0
        assert cf.stats(conn)["total"] >= 1
    finally:
        conn.close()
