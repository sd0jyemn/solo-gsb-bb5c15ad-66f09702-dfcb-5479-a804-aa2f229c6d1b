"""两封已归档邮件的 MIME 树结构对比（纯只读）。

在两封邮件已保存的节点行上做确定性的递归对齐与差异计算，不重新解析
原始字节、也不改写任何记录。对比的三个面：

1. **规范化 Content-Type**——重新解析归档时保存的 Content-Type
   头，取小写媒体类型与参数（剔除打包/传输噪音 ``boundary`` 与
   文本节点的 ``charset``，与指纹口径一致），参数按名排序；
2. **头字段**——头名小写、值做线性空白压缩，剔除
   Content-Type（单独比较）与 Content-Transfer-Encoding
   （传输噪音）；重复头按同名分组逐位对齐，新增/删除/变更；
3. **解码叶子摘要**——文本叶子取规范化文本（换行已归一、NFC），
   非文本叶子取解码字节 SHA-256 与长度，容器节点无叶子摘要。

节点对齐不把物化路径当作身份：同一层的子节点先按**子树内容指纹**
配对——内容完全相同的子树即使发生重排也能识别为“无差异”。指纹
一一对应，或同指纹重复在两侧等重（n==n）时，按文档序配对（内容
完全相同，配对方式无歧义）；同指纹重复在两侧都存在但数量不等、
一一对应关系不唯一时给出 ``ambiguous`` 记录（含双方候选路径），
少量侧之外的多余子树同时按 added/removed 呈现，保证差异完整。
其余没有指纹对应的节点按 MIME 文档序逐位配对（内容变更场景），
数量不等时多出的整棵子树按节点记为 added/removed。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .mimeparser import parse_content_type

# 不参与头字段对比的结构/传输头：
# content-type 单独做规范化对比；content-transfer-encoding 与
# boundary/charset 一样属于传输噪音（归档叶子已解码）。
_HEADER_SKIP = {"content-type", "content-transfer-encoding"}

# 排序时“这一侧不存在”的占位（大于任何真实兄弟位置）。
_SIDE_END = (1 << 30,)


def _order_prefix(path: str) -> tuple[int, ...]:
    """物化路径的文档序前缀键：``"" -> ()``、``"2/1" -> (2, 1)``。

    用前缀而非扁平序号，使“整棵新增/删除子树”的全部节点在输出中
    连续排列（子节点排在父节点之后、叔叔节点之前）。
    """

    return tuple(int(seg) for seg in path.split("/")) if path else ()


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class FieldDifference:
    facet: str  # content_type | header | leaf_digest
    header: Optional[str] = None  # 仅 facet == header：小写头名
    change: Optional[str] = None  # 仅 facet == header：added | removed | modified
    before: object = None
    after: object = None


@dataclass
class NodeDifference:
    change: str  # changed | added | removed | ambiguous
    path_a: Optional[str] = None
    path_b: Optional[str] = None
    content_type_a: Optional[str] = None
    content_type_b: Optional[str] = None
    fields: list[FieldDifference] = field(default_factory=list)
    # change == ambiguous：双方同层无法唯一配对的全部候选路径
    candidates_a: list[str] = field(default_factory=list)
    candidates_b: list[str] = field(default_factory=list)


@dataclass
class CompareResult:
    identical: bool
    differences: list[NodeDifference]


# ---------------------------------------------------------------------------
# 规范化辅助
# ---------------------------------------------------------------------------


def _stable_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _normalize_header_wsp(text: str) -> str:
    """与指纹口径一致：连续 WSP 压成一个空格并去两端。"""

    return " ".join(text.split())


def normalize_content_type(row: sqlite3.Row) -> dict:
    """节点的规范化 Content-Type：``{"media_type", "params"}``。

    重新解析归档时保存的 Content-Type 头（头值已完成 RFC 2047/2231
    解码，latin-1 回灌对 ASCII 结构头无损），剔除 ``boundary`` 与
    ``charset`` 后参数按名排序。归档数据损坏（理论上不会发生，
    入库时已严格校验）时回退为存储的媒体类型、不带参数。
    """

    stored_value = None
    for name, value in json.loads(row["headers_json"]):
        if name.lower() == "content-type":
            stored_value = value
            break

    media_type = row["content_type"]
    params: dict[str, str] = {}
    if stored_value is not None:
        try:
            ct = parse_content_type(stored_value.encode("latin-1"), row["path"])
            media_type = ct.media_type
            params = {
                key: val
                for key, val in ct.params.items()
                if key not in ("boundary", "charset")
            }
        except Exception:
            # 历史库/脏数据兜底：仍按存储媒体类型比较，不使接口失败。
            media_type = row["content_type"]
    return {"media_type": media_type, "params": dict(sorted(params.items()))}


def _normalize_headers(row: sqlite3.Row) -> dict[str, list[str]]:
    """头名小写分组、值压缩线性空白，保留同名头的重复与文档序。"""

    grouped: dict[str, list[str]] = {}
    for name, value in json.loads(row["headers_json"]):
        key = name.lower()
        if key in _HEADER_SKIP:
            continue
        grouped.setdefault(key, []).append(_normalize_header_wsp(value))
    return grouped


def _leaf_digest(row: sqlite3.Row) -> dict:
    """叶子摘要；容器（有子节点）返回空 dict。"""

    if json.loads(row["children_json"]):
        return {}
    if row["kind"] == "text":
        text = row["text_content"] if row["text_content"] is not None else ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return {"kind": "text", "length": len(text), "sha256": digest}
    # 非文本叶子：归档时已算好 blob_sha256 / blob_length
    return {
        "kind": "blob",
        "length": row["blob_length"] if row["blob_length"] is not None else 0,
        "sha256": row["blob_sha256"]
        if row["blob_sha256"] is not None
        else hashlib.sha256(b"").hexdigest(),
    }


# ---------------------------------------------------------------------------
# 面（facet）差异
# ---------------------------------------------------------------------------


def _diff_headers(
    headers_a: dict[str, list[str]],
    headers_b: dict[str, list[str]],
) -> list[FieldDifference]:
    fields: list[FieldDifference] = []
    for name in sorted(set(headers_a) | set(headers_b)):
        va = headers_a.get(name, [])
        vb = headers_b.get(name, [])
        if va == vb:
            continue
        if not va:
            change = "added"
        elif not vb:
            change = "removed"
        else:
            change = "modified"
        fields.append(
            FieldDifference(
                facet="header",
                header=name,
                change=change,
                before=va,
                after=vb,
            )
        )
    return fields


def _diff_node(a: sqlite3.Row, b: sqlite3.Row) -> list[FieldDifference]:
    """两个已配对节点自身（不含子树）的面差异。"""

    fields: list[FieldDifference] = []

    ct_a = normalize_content_type(a)
    ct_b = normalize_content_type(b)
    if ct_a != ct_b:
        fields.append(
            FieldDifference(facet="content_type", before=ct_a, after=ct_b)
        )

    fields.extend(_diff_headers(_normalize_headers(a), _normalize_headers(b)))

    leaf_a = _leaf_digest(a)
    leaf_b = _leaf_digest(b)
    if leaf_a != leaf_b:
        fields.append(
            FieldDifference(facet="leaf_digest", before=leaf_a, after=leaf_b)
        )
    return fields


# ---------------------------------------------------------------------------
# 子树内容指纹（用于同层重排配对）
# ---------------------------------------------------------------------------


def _facet_key(row: sqlite3.Row) -> str:
    """节点自身三面的稳定序列化，作为子树指纹的输入。"""

    key = _stable_json(
        {
            "content_type": normalize_content_type(row),
            "headers": _normalize_headers(row),
            "leaf": _leaf_digest(row),
        }
    )
    return hashlib.sha256(key).hexdigest()


def _compute_fingerprints(
    by_path: dict[str, sqlite3.Row],
) -> dict[str, str]:
    """后序计算每个节点的子树内容指纹（文档序子指纹参与）。"""

    fingerprints: dict[str, str] = {}

    def build(path: str) -> str:
        row = by_path[path]
        parts = [_facet_key(row)]
        for child in json.loads(row["children_json"]):
            parts.append(build(child))  # children 已是文档序
        fp = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
        fingerprints[path] = fp
        return fp

    build("")
    return fingerprints


# ---------------------------------------------------------------------------
# 递归对齐
# ---------------------------------------------------------------------------


def _emit_subtree(
    row: sqlite3.Row,
    by_path: dict[str, sqlite3.Row],
    side: str,
) -> list[NodeDifference]:
    """整棵子树按节点记 added/removed（先根后叶）。"""

    if side == "a":
        diff = NodeDifference(
            change="removed",
            path_a=row["path"],
            content_type_a=row["content_type"],
        )
    else:
        diff = NodeDifference(
            change="added",
            path_b=row["path"],
            content_type_b=row["content_type"],
        )
    out = [diff]
    for child in json.loads(row["children_json"]):
        out.extend(_emit_subtree(by_path[child], by_path, side))
    return out


def _align_pair(
    row_a: sqlite3.Row,
    row_b: sqlite3.Row,
    by_a: dict[str, sqlite3.Row],
    by_b: dict[str, sqlite3.Row],
    fp_a: dict[str, str],
    fp_b: dict[str, str],
) -> list[NodeDifference]:
    """一对已配对节点：自身面差异 + 递归对齐直接子件。"""

    out: list[NodeDifference] = []
    fields = _diff_node(row_a, row_b)
    if fields:
        # 仅当节点自身的某个面不同才产出 changed；自身相同、差异只在
        # 后代的节点不占输出（后代差异各自给出）。
        out.append(
            NodeDifference(
                change="changed",
                path_a=row_a["path"],
                path_b=row_b["path"],
                content_type_a=row_a["content_type"],
                content_type_b=row_b["content_type"],
                fields=fields,
            )
        )
    out.extend(_align_children(row_a, row_b, by_a, by_b, fp_a, fp_b))
    return out


def _align_children(
    parent_a: Optional[sqlite3.Row],
    parent_b: Optional[sqlite3.Row],
    by_a: dict[str, sqlite3.Row],
    by_b: dict[str, sqlite3.Row],
    fp_a: dict[str, str],
    fp_b: dict[str, str],
) -> list[NodeDifference]:
    children_a = json.loads(parent_a["children_json"]) if parent_a else []
    children_b = json.loads(parent_b["children_json"]) if parent_b else []

    groups_a: dict[str, list[str]] = {}
    groups_b: dict[str, list[str]] = {}
    for p in children_a:
        groups_a.setdefault(fp_a[p], []).append(p)
    for p in children_b:
        groups_b.setdefault(fp_b[p], []).append(p)

    out: list[NodeDifference] = []
    paired_a: set[str] = set()
    paired_b: set[str] = set()

    for fp in sorted(set(groups_a) | set(groups_b)):
        pa = groups_a.get(fp, [])
        pb = groups_b.get(fp, [])
        if pa and pb and len(pa) == len(pb):
            # 两侧都存在且等重：1:1 唯一配对，或同指纹重复在两侧
            # 等重（子树内容完全相同，文档序逐对配对，结果无歧义）。
            for x, y in zip(pa, pb):
                paired_a.add(x)
                paired_b.add(y)
                out.extend(_align_pair(by_a[x], by_b[y], by_a, by_b,
                                       fp_a, fp_b))
        elif pa and pb:
            # 同指纹重复在两侧都存在但数量不等：一一对应关系不唯一，
            # 给出 ambiguous 记录双方候选；数量较少一侧之外的多余
            # 子树按 added/removed 呈现，保证差异完整（具体哪一份与
            # 哪一份对应仍以 ambiguous 为准）。
            out.append(
                NodeDifference(
                    change="ambiguous",
                    candidates_a=pa,
                    candidates_b=pb,
                )
            )
            common = min(len(pa), len(pb))
            for x, y in zip(pa[:common], pb[:common]):
                paired_a.add(x)
                paired_b.add(y)
                out.extend(_align_pair(by_a[x], by_b[y], by_a, by_b,
                                       fp_a, fp_b))
            # 多余节点已作为 added/removed 输出，也需登记为已消费，
            # 避免进入后面的逐位配对再报一次。
            paired_a.update(pa[common:])
            paired_b.update(pb[common:])
            for x in pa[common:]:
                out.extend(_emit_subtree(by_a[x], by_a, "a"))
            for y in pb[common:]:
                out.extend(_emit_subtree(by_b[y], by_b, "b"))
        # 仅一侧出现的指纹不构成“配对歧义”：留给下面的文档序逐位
        # 配对（内容变更）或 added/removed 处理。

    # 无指纹对应的剩余节点：按文档序逐位配对（内容变更），多出的
    # 整棵子树按节点记 added/removed。
    rest_a = [p for p in children_a if p not in paired_a]
    rest_b = [p for p in children_b if p not in paired_b]
    common = min(len(rest_a), len(rest_b))
    for i in range(common):
        out.extend(
            _align_pair(by_a[rest_a[i]], by_b[rest_b[i]], by_a, by_b,
                        fp_a, fp_b)
        )
    for p in rest_a[common:]:
        out.extend(_emit_subtree(by_a[p], by_a, "a"))
    for p in rest_b[common:]:
        out.extend(_emit_subtree(by_b[p], by_b, "b"))
    return out


def _sort_key(diff: NodeDifference) -> tuple:
    """差异的稳定排序键：双方文档序前缀，缺失侧用末端占位。"""

    if diff.change == "removed":
        return _order_prefix(diff.path_a), _SIDE_END
    if diff.change == "added":
        return _SIDE_END, _order_prefix(diff.path_b)
    if diff.change == "ambiguous":
        ka = min((_order_prefix(p) for p in diff.candidates_a),
                 default=_SIDE_END)
        kb = min((_order_prefix(p) for p in diff.candidates_b),
                 default=_SIDE_END)
        return ka, kb
    return _order_prefix(diff.path_a), _order_prefix(diff.path_b)


def compare_trees(
    nodes_a: list[sqlite3.Row],
    nodes_b: list[sqlite3.Row],
) -> CompareResult:
    """对比两封邮件的已保存 MIME 节点，差异按文档序稳定输出。"""

    by_a = {row["path"]: row for row in nodes_a}
    by_b = {row["path"]: row for row in nodes_b}
    fp_a = _compute_fingerprints(by_a)
    fp_b = _compute_fingerprints(by_b)

    differences = _align_pair(by_a[""], by_b[""], by_a, by_b, fp_a, fp_b)
    differences.sort(key=_sort_key)
    return CompareResult(
        identical=not differences,
        differences=differences,
    )
