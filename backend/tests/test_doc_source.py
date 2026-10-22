import pytest

from app.services.doc_source import MAX_DOC_CHARS, load_source


class FakeLark:
    async def docs_get(self, token):
        return {"title": "来自 CLI 的标题"}

    async def docs_export_markdown(self, token):
        return "# 标题\n正文内容"


class Ctx:
    def __init__(self, lark=None):
        self.lark = lark or FakeLark()
        self.lines = []

    async def log(self, level, msg):
        self.lines.append(msg)


@pytest.fixture
def indexed(monkeypatch):
    """把索引查询换成固定的一条，避免测试依赖本机数据库。"""
    from app.services import doc_source

    async def fake_list_assets(**kw):
        return [{"asset_id": "T", "type": "docx", "title": "索引里的标题",
                 "owner": "o", "space": "s", "updated": "u", "url": "x"}]

    monkeypatch.setattr(doc_source.index_service, "list_assets", fake_list_assets)


@pytest.mark.asyncio
async def test_load_source_returns_markdown(indexed):
    src = await load_source(Ctx(), "T", {})
    assert src["markdown"] == "# 标题\n正文内容"
    assert src["asset_type"] == "docx"
    assert src["token"] == "T"


@pytest.mark.asyncio
async def test_load_source_truncates_overlong_body(indexed):
    class Huge(FakeLark):
        async def docs_export_markdown(self, token):
            return "字" * (MAX_DOC_CHARS + 5000)

    src = await load_source(Ctx(Huge()), "T", {})
    assert len(src["markdown"]) <= MAX_DOC_CHARS + 40
    assert "已截断" in src["markdown"]


@pytest.mark.asyncio
async def test_load_source_rejects_unsupported_type(indexed):
    with pytest.raises(ValueError, match="暂不支持"):
        await load_source(Ctx(), "T", {"asset_type": "mindnote"})
