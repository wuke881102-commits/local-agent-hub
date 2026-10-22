from app.services.fact_check import filter_real_metrics, table_is_real

SRC = "本季度客服响应效率提升 80%，拦截恶意请求 1200 万次，覆盖 36 个业务线。"


def test_filter_real_metrics_keeps_numbers_found_in_source():
    got = filter_real_metrics([{"value": "↑80%", "label": "效率"},
                               {"value": "1200 万", "label": "拦截"}], SRC)
    assert [m["label"] for m in got] == ["效率", "拦截"]


def test_filter_real_metrics_drops_invented_numbers():
    got = filter_real_metrics([{"value": "95%", "label": "编的"},
                               {"value": "80%", "label": "真的"}], SRC)
    assert [m["label"] for m in got] == ["真的"]


def test_filter_real_metrics_drops_metrics_without_digits():
    assert filter_real_metrics([{"value": "显著提升", "label": "x"}], SRC) == []


def test_filter_real_metrics_ignores_single_digit_matches():
    """单个数字在长文里到处都能撞上，不能拿来当证据。"""
    assert filter_real_metrics([{"value": "3 倍", "label": "x"}], SRC + " 3 个人") == []


def test_table_is_real_when_most_cells_appear_in_source():
    src = "| 阶段 | 负责人 |\n| 开发 | 张三 |\n| 测试 | 李四 |"
    assert table_is_real(["阶段", "负责人"], [["开发", "张三"], ["测试", "李四"]], src)


def test_table_is_real_rejects_invented_table():
    """模型重组时可能自己「整理」出一张原文没有的表 —— 过半单元格对不上就不要。"""
    src = "| 阶段 | 负责人 |\n| 开发 | 张三 |"
    assert not table_is_real(["季度", "营收"], [["Q1", "300 万"], ["Q2", "420 万"]], src)


def test_table_is_real_normalizes_whitespace_and_punctuation():
    src = "路径：Source 到 Sink 真实连通"      # 表头也得在原文里，否则只命中一半
    assert table_is_real(["路径"], [["Source到Sink真实连通"]], src)


def test_table_is_real_empty_table_is_not_real():
    assert not table_is_real([], [], SRC)
