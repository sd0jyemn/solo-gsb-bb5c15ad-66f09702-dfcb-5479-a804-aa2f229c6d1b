"""POST /messages/compare：两封已归档邮件的 MIME 树只读对比。"""

import base64

import pytest

from conftest import make_multipart, text_part


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def text_leaf(body: str, extra: bytes = b"", charset: str = "utf-8") -> bytes:
    return text_part(body, charset=charset, headers=extra)


def blob_leaf(raw: bytes, content_type: str = "application/octet-stream",
              extra: bytes = b"") -> bytes:
    b64 = base64.b64encode(raw)
    return (
        f"Content-Type: {content_type}\r\n".encode()
        + b"Content-Transfer-Encoding: base64\r\n"
        + extra
        + b"\r\n"
        + b64
    )


def mixed(parts: list[bytes], boundary: str = "B") -> bytes:
    return (
        f"Content-Type: multipart/mixed; boundary={boundary}\r\n\r\n".encode()
        + make_multipart(parts, boundary)
    )


def alternative(parts: list[bytes], boundary: str = "B") -> bytes:
    return (
        f"Content-Type: multipart/alternative; boundary={boundary}\r\n\r\n".encode()
        + make_multipart(parts, boundary)
    )


def post(client, a: int, b: int):
    return client.post(
        "/messages/compare", json={"message_id_a": a, "message_id_b": b}
    )


def archive(client, data: bytes) -> int:
    r = client.post("/messages", content=data)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def by_change(differences, change):
    return [d for d in differences if d["change"] == change]


# ---------------------------------------------------------------------------
# 相同 / 规范化等价
# ---------------------------------------------------------------------------


def test_identical_messages(client):
    msg = (
        b"From: alice@example.com\r\nSubject: hi\r\n\r\n"
        b"body line\r\n"
    )
    a = archive(client, msg)
    b = archive(client, msg)  # 同指纹幂等 → 同一 id，无法自比
    assert a == b


def test_compare_single_leaf_identical_different_raw(client):
    # 逐字节相同的邮件重复归档是幂等的，只得到同一个 id。
    msg = b"Subject: a\r\n\r\nx\r\n"
    a = archive(client, msg)
    b = archive(client, msg)
    assert a == b
    # 用两个仅 Subject 不同的单叶邮件验证最小 changed 输出
    other = archive(client, b"Subject: a2\r\n\r\nx\r\n")
    body = post(client, a, other).json()
    assert body["identical"] is False
    changed = by_change(body["differences"], "changed")
    assert len(changed) == 1
    assert changed[0]["path_a"] == "" and changed[0]["path_b"] == ""


def test_compare_transport_normalization_identical(client):
    # 等价 CTE / 换行 / boundary / 头大小写 → 同指纹 → 同 id，
    # 这里直接验证两种不同打包得到相同 id（即差异为空的前置不变量）。
    m1 = mixed([text_leaf("hello"), text_leaf("world")], "BOUND1")
    m2 = mixed([text_leaf("hello"), text_leaf("world")], "BOUND2")
    assert archive(client, m1) == archive(client, m2)


# ---------------------------------------------------------------------------
# 头字段差异
# ---------------------------------------------------------------------------


def test_header_added(client):
    a = archive(client, mixed([text_leaf("hello")]))
    b = archive(
        client, mixed([text_leaf("hello", b"X-Custom: foo\r\n")])
    )
    body = post(client, a, b).json()
    assert body["identical"] is False
    changed = by_change(body["differences"], "changed")
    assert len(changed) == 1
    d = changed[0]
    assert d["path_a"] == "1" and d["path_b"] == "1"
    header_diffs = [f for f in d["fields"] if f["facet"] == "header"]
    assert len(header_diffs) == 1
    hd = header_diffs[0]
    assert hd["header"] == "x-custom"
    assert hd["change"] == "added"
    assert hd["before"] == []
    assert hd["after"] == ["foo"]


def test_header_removed_and_modified(client):
    a = archive(
        client, mixed([text_leaf("hello", b"X-A: 1\r\nX-B: keep\r\n")])
    )
    b = archive(
        client, mixed([text_leaf("hello", b"X-B: changed\r\n")])
    )
    body = post(client, a, b).json()
    fields = body["differences"][0]["fields"]
    headers = {f["header"]: f for f in fields if f["facet"] == "header"}
    assert set(headers) == {"x-a", "x-b"}
    assert headers["x-a"]["change"] == "removed"
    assert headers["x-a"]["after"] == []
    assert headers["x-a"]["before"] == ["1"]
    assert headers["x-b"]["change"] == "modified"
    assert headers["x-b"]["before"] == ["keep"]
    assert headers["x-b"]["after"] == ["changed"]


def test_header_name_case_and_folding_normalized(client):
    # 头名大小写不同、线性空白差异在指纹层即等价（幂等返回同一 id）
    a = archive(client, b"X-MixedCase:   a   b\tc \r\n\r\nx\r\n")
    b = archive(client, b"x-mixedcase: a b c\r\n\r\nx\r\n")
    assert a == b


def test_duplicate_headers_diff(client):
    a = archive(client, b"Received: a\r\nReceived: b\r\n\r\nx\r\n")
    b = archive(client, b"Received: a\r\nReceived: c\r\n\r\nx\r\n")
    body = post(client, a, b).json()
    fields = body["differences"][0]["fields"]
    rcvd = [f for f in fields if f["facet"] == "header"]
    assert len(rcvd) == 1
    assert rcvd[0]["header"] == "received"
    assert rcvd[0]["change"] == "modified"
    assert rcvd[0]["before"] == ["a", "b"]
    assert rcvd[0]["after"] == ["a", "c"]


def test_header_reorder_not_a_difference(client):
    # 头字段整体重排（非重复头）不应报告差异
    a = archive(client, b"X-A: 1\r\nX-B: 2\r\n\r\nx\r\n")
    b = archive(client, b"X-B: 2\r\nX-A: 1\r\n\r\nx\r\n")
    assert post(client, a, b).json()["identical"] is True


def test_cte_header_not_compared(client):
    # Content-Transfer-Encoding 是传输噪音；base64 与 8bit 打包的
    # 同内容叶子在指纹层即等价（幂等返回同一 id）。
    a = archive(
        client,
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\nh\xc3\xa9llo",
    )
    leaf_b = (
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        + base64.b64encode("héllo".encode())
        + b"\r\n"
    )
    b = archive(client, leaf_b)
    assert a == b  # 同指纹：直接证明 CTE 与 charset 不影响对比


# ---------------------------------------------------------------------------
# Content-Type 规范化
# ---------------------------------------------------------------------------


def test_content_type_subtype_changed(client):
    a = archive(client, mixed([text_leaf("x")]))
    b = archive(client, alternative([text_leaf("x")]))
    body = post(client, a, b).json()
    ct = [
        f for d in body["differences"]
        for f in d["fields"] if f["facet"] == "content_type"
    ]
    assert len(ct) == 1
    assert ct[0]["before"]["media_type"] == "multipart/mixed"
    assert ct[0]["after"]["media_type"] == "multipart/alternative"


def test_content_type_param_changed_but_boundary_ignored(client):
    # 同层叶子的 Content-Type 参数变化（name）能检出；boundary 不参与。
    leaf_a = (
        b"Content-Type: text/plain; charset=utf-8; name=a.txt\r\n\r\nx"
    )
    leaf_b = (
        b"Content-Type: text/plain; charset=utf-8; name=b.txt\r\n\r\nx"
    )
    a = archive(client, leaf_a)
    b = archive(client, leaf_b)
    body = post(client, a, b).json()
    ct = body["differences"][0]["fields"][0]
    assert ct["facet"] == "content_type"
    assert ct["before"]["params"] == {"name": "a.txt"}
    assert ct["after"]["params"] == {"name": "b.txt"}
    assert "boundary" not in ct["before"]["params"]
    assert "charset" not in ct["before"]["params"]


def test_content_type_param_order_normalized(client):
    a = archive(
        client,
        b'Content-Type: text/plain; charset=utf-8; name="x"; format=flowed\r\n\r\nb',
    )
    b = archive(
        client,
        b'Content-Type: text/plain; format=flowed; name="x"; charset=utf-8\r\n\r\nb',
    )
    assert a == b  # 同指纹即规范化等价


# ---------------------------------------------------------------------------
# 叶子摘要
# ---------------------------------------------------------------------------


def test_text_leaf_digest_changed(client):
    a = archive(client, text_leaf("hello"))
    b = archive(client, text_leaf("HELLO!"))
    body = post(client, a, b).json()
    leaf = body["differences"][0]["fields"][0]
    assert leaf["facet"] == "leaf_digest"
    assert leaf["before"]["kind"] == "text"
    assert leaf["before"]["length"] == 5
    assert leaf["after"]["length"] == 6
    assert leaf["before"]["sha256"] != leaf["after"]["sha256"]


def test_blob_leaf_digest_changed(client):
    a = archive(client, blob_leaf(b"\x00\x01\x02"))
    b = archive(client, blob_leaf(b"\x00\x01\x03"))
    body = post(client, a, b).json()
    leaf = body["differences"][0]["fields"][0]
    assert leaf["facet"] == "leaf_digest"
    assert leaf["before"] == {
        "kind": "blob",
        "length": 3,
        "sha256": __import__("hashlib").sha256(b"\x00\x01\x02").hexdigest(),
    }
    assert leaf["after"]["kind"] == "blob"


def test_leaf_kind_changed_text_to_blob(client):
    a = archive(client, text_leaf("abc"))
    b = archive(client, blob_leaf(b"abc"))
    body = post(client, a, b).json()
    fac = [f["facet"] for f in body["differences"][0]["fields"]]
    assert "content_type" in fac
    assert "leaf_digest" in fac


# ---------------------------------------------------------------------------
# 结构：新增 / 删除子树
# ---------------------------------------------------------------------------


def test_added_part(client):
    a = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    b = archive(
        client,
        mixed([text_leaf("hello"), text_leaf("world"), text_leaf("extra")]),
    )
    body = post(client, a, b).json()
    added = by_change(body["differences"], "added")
    assert len(added) == 1
    assert added[0]["path_b"] == "3"
    assert added[0]["content_type_b"] == "text/plain"
    assert by_change(body["differences"], "removed") == []
    assert by_change(body["differences"], "changed") == []


def test_removed_part_reverse_direction(client):
    a = archive(
        client,
        mixed([text_leaf("hello"), text_leaf("world"), text_leaf("extra")]),
    )
    b = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    body = post(client, a, b).json()
    removed = by_change(body["differences"], "removed")
    assert len(removed) == 1
    assert removed[0]["path_a"] == "3"


def test_middle_insertion_positional(client):
    # 在两个不同内容的兄弟中间插入：位置变化的弟弟按指纹锚定，
    # 不应出现 world 被误报。
    a = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    b = archive(
        client,
        mixed([text_leaf("hello"), text_leaf("middle"), text_leaf("world")]),
    )
    body = post(client, a, b).json()
    added = by_change(body["differences"], "added")
    changed = by_change(body["differences"], "changed")
    assert [d["path_b"] for d in added] == ["2"]
    assert changed == []


def test_added_nested_subtree_emits_all_nodes(client):
    inner = mixed([text_leaf("x"), text_leaf("y")], "IN")
    inner_part = (
        b"Content-Type: multipart/mixed; boundary=IN\r\n\r\n"
        + make_multipart([text_leaf("x"), text_leaf("y")], "IN")
    )
    a = archive(client, mixed([text_leaf("keep")]))
    b = archive(client, mixed([text_leaf("keep"), inner_part]))
    body = post(client, a, b).json()
    added = by_change(body["differences"], "added")
    # 容器 + 两个叶子，按先根后叶排序
    assert [d["path_b"] for d in added] == ["2", "2/1", "2/2"]
    assert added[0]["content_type_b"] == "multipart/mixed"


# ---------------------------------------------------------------------------
# 重排：按内容指纹配对
# ---------------------------------------------------------------------------


def test_parts_reordered_identical(client):
    a = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    b = archive(client, mixed([text_leaf("world"), text_leaf("hello")]))
    body = post(client, a, b).json()
    assert body["identical"] is True
    assert body["differences"] == []


def test_reorder_with_change_anchors_by_fingerprint(client):
    # A: [hello, world]，B: [world!, hello]——world 内容变了且发生重排。
    # hello 子树指纹相同应跨位置锚定（无差异），world 与 world! 按
    # 文档序逐位配对报 changed，而不是把 hello↔world 误配。
    a = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    b = archive(
        client, mixed([text_leaf("world!!"), text_leaf("hello")])
    )
    body = post(client, a, b).json()
    changed = by_change(body["differences"], "changed")
    assert len(changed) == 1
    assert changed[0]["path_a"] == "2"
    assert changed[0]["path_b"] == "1"
    assert changed[0]["fields"][0]["facet"] == "leaf_digest"


def test_identical_nested_subtrees_reorder(client):
    inner1 = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n"
        + make_multipart([text_leaf("p"), text_leaf("h")], "IN")
    )
    inner2 = (
        b"Content-Type: multipart/alternative; boundary=IN\r\n\r\n"
        + make_multipart([text_leaf("h"), text_leaf("p")], "IN")
    )
    a = archive(client, mixed([text_leaf("top"), inner1]))
    b = archive(client, mixed([inner2, text_leaf("top")]))
    assert post(client, a, b).json()["identical"] is True


# ---------------------------------------------------------------------------
# 歧义：同指纹重复数量不等
# ---------------------------------------------------------------------------


def test_ambiguous_duplicate_counts(client):
    # A 有 1 个 hello，B 有 2 个 hello（另一个兄弟 world 相同）
    a = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    b = archive(
        client,
        mixed([text_leaf("hello"), text_leaf("hello"), text_leaf("world")]),
    )
    body = post(client, a, b).json()
    ambiguous = by_change(body["differences"], "ambiguous")
    added = by_change(body["differences"], "added")
    assert len(ambiguous) == 1
    assert ambiguous[0]["candidates_a"] == ["1"]
    assert ambiguous[0]["candidates_b"] == ["1", "2"]
    # 多余的一份同时以 added 呈现，差异完整
    assert [d["path_b"] for d in added] == ["2"]


def test_equal_duplicate_counts_not_ambiguous(client):
    # 两侧都有两个相同的 hello：等重重复按文档序配对，无歧义。
    # 用无关头区分两封邮件（避免同指纹幂等），重复结构保持一致。
    a = archive(
        client,
        b"X-Tag: one\r\n" + mixed([text_leaf("hello"), text_leaf("hello")]),
    )
    b = archive(
        client,
        b"X-Tag: two\r\n"
        + mixed([text_leaf("hello"), text_leaf("hello")], "OTHER"),
    )
    body = post(client, a, b).json()
    # 唯一差异在根节点的 X-Tag，子层等重重复既无歧义也无节点差异
    assert by_change(body["differences"], "ambiguous") == []
    assert by_change(body["differences"], "added") == []
    assert by_change(body["differences"], "removed") == []
    changed = by_change(body["differences"], "changed")
    assert len(changed) == 1
    assert changed[0]["path_a"] == "" == changed[0]["path_b"]


def test_ambiguous_reverse_direction(client):
    a = archive(
        client,
        mixed([text_leaf("hello"), text_leaf("hello"), text_leaf("world")]),
    )
    b = archive(client, mixed([text_leaf("hello"), text_leaf("world")]))
    body = post(client, a, b).json()
    ambiguous = by_change(body["differences"], "ambiguous")
    removed = by_change(body["differences"], "removed")
    assert ambiguous[0]["candidates_a"] == ["1", "2"]
    assert ambiguous[0]["candidates_b"] == ["1"]
    assert [d["path_a"] for d in removed] == ["2"]


# ---------------------------------------------------------------------------
# 输出稳定性 / 路径
# ---------------------------------------------------------------------------


def test_differences_sorted_by_document_order(client):
    parts_a = [text_leaf(f"p{i}") for i in range(4)]
    parts_b = list(parts_a)
    parts_b[0] = text_leaf("CHANGED0")
    parts_b[2] = text_leaf("CHANGED2")
    a = archive(client, mixed(parts_a))
    b = archive(client, mixed(parts_b))
    body = post(client, a, b).json()
    changed = by_change(body["differences"], "changed")
    assert [d["path_a"] for d in changed] == ["1", "3"]


def test_repeated_compare_is_stable(client):
    a = archive(
        client,
        mixed([text_leaf("a"), text_leaf("b"), text_leaf("c")]),
    )
    b = archive(
        client,
        mixed([text_leaf("a"), text_leaf("x"), text_leaf("c"),
               text_leaf("d")]),
    )
    r1 = post(client, a, b).json()
    r2 = post(client, a, b).json()
    assert r1 == r2


def test_response_carries_both_summaries(client):
    a = archive(client, text_leaf("one"))
    b = archive(client, text_leaf("two"))
    body = post(client, a, b).json()
    assert body["a"]["id"] == a
    assert body["b"]["id"] == b
    assert body["a"]["fingerprint"] != body["b"]["fingerprint"]


def test_message_rfc822_nested_paths(client):
    # 封装报文内部节点路径（1/1）也能正确对齐与比较
    inner_a = b"Subject: inner\r\n\r\ninside-a\r\n"
    inner_b = b"Subject: inner\r\n\r\ninside-b\r\n"
    wrap_a = (
        b"Content-Type: message/rfc822\r\n\r\n" + inner_a
    )
    wrap_b = (
        b"Content-Type: message/rfc822\r\n\r\n" + inner_b
    )
    a = archive(client, mixed([text_leaf("top"), wrap_a]))
    b = archive(client, mixed([text_leaf("top"), wrap_b]))
    body = post(client, a, b).json()
    changed = by_change(body["differences"], "changed")
    assert [d["path_a"] for d in changed] == ["2/1"]
    assert changed[0]["path_b"] == "2/1"


# ---------------------------------------------------------------------------
# 错误情况（4xx）
# ---------------------------------------------------------------------------


def test_same_id_returns_400(client):
    mid = archive(client, text_leaf("x"))
    r = post(client, mid, mid)
    assert r.status_code == 400
    assert "message_id_a" in r.json()["detail"]


def test_same_nonexistent_id_still_400(client):
    # id 相同优先判 400（即使该 id 不存在）
    r = post(client, 4242, 4242)
    assert r.status_code == 400


def test_missing_id_returns_404(client):
    mid = archive(client, text_leaf("x"))
    r = post(client, mid, 999)
    assert r.status_code == 404
    assert "999" in r.json()["detail"]
    r = post(client, 998, 999)
    assert r.status_code == 404
    assert "998" in r.json()["detail"]
    assert "999" in r.json()["detail"]


def test_malformed_body_422(client):
    r = client.post("/messages/compare", json={"message_id_a": 1})
    assert r.status_code == 422
    r = client.post("/messages/compare", json={"a": 1, "b": 2})
    assert r.status_code == 422
    r = client.post(
        "/messages/compare",
        json={"message_id_a": "x", "message_id_b": 2},
    )
    assert r.status_code == 422


def test_compare_is_read_only(client):
    a = archive(client, mixed([text_leaf("hello")]))
    b = archive(client, mixed([text_leaf("world")]))
    before = client.get("/messages").json()
    for _ in range(2):
        r = post(client, a, b)
        assert r.status_code == 200
    after = client.get("/messages").json()
    assert before["total"] == after["total"]
    # 节点、附件计数也不变
    assert len(client.get(f"/messages/{a}/nodes").json()["nodes"]) == 2
    assert len(client.get(f"/messages/{b}/nodes").json()["nodes"]) == 2
