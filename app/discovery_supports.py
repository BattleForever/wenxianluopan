"""P1 发现依据（supports）：记录任务遍历时实际观察到的去重发现依据。

规则来源：parallel_v01/00_并行规则与接口.md "A → C：发现依据"。
- 统计对象是本任务实际遍历时观察到的去重发现依据，唯一键
  (seed, parent, id, relation)；同一依据反复响应不增加计数。
- 不枚举所有简单路径，不声称独立证据数量，不将计数自动转为论文质量分。
- 每篇（每个结果节点）最多保留 50 条依据；超过时 support_truncated=true，
  support_count 为实际存储的去重数，界面标"至少 N 条已记录依据"。
- 已见过的节点再次被其他父节点观察到时，同样补充新依据：
  结果去重不得丢掉另一个父节点的观察（多种子/多父归属按实际观察记录，
  不声称穷尽）。
"""

MAX_SUPPORTS = 50


class SupportLedger:
    """单次探索任务的发现依据账本。

    observe() 返回是否新存储了一条依据；attach() 把每个结果节点的
    support_count / support_truncated / supports 写入结果列表（就地修改）。
    """

    def __init__(self, cap: int = MAX_SUPPORTS):
        if cap < 0:
            raise ValueError('supports 上限不能为负')
        self.cap = int(cap)
        self._entries = {}   # node_id -> list[dict]，按首次观察顺序
        self._keys = {}      # node_id -> set[(seed, parent, id, relation)]
        self._truncated = set()

    def observe(self, node_id, seed, parent, relation, depth,
                edge_source=None, edge_from=None, edge_to=None) -> bool:
        """记录一次实际观察到的发现依据（node 经 parent 按 relation 发现）。

        同一 (seed, parent, id, relation) 重复响应不累加；每篇超过 cap 条
        时只标 truncated，不再存储。
        """
        nid = int(node_id)
        key = (int(seed), int(parent), nid, str(relation))
        keys = self._keys.setdefault(nid, set())
        if key in keys:
            return False  # 同一依据重复响应：不增加计数
        keys.add(key)
        entries = self._entries.setdefault(nid, [])
        if len(entries) >= self.cap:
            self._truncated.add(nid)
            return False
        entries.append({
            'seed': int(seed),
            'parent': int(parent),
            'id': nid,
            'relation': str(relation),
            'depth': int(depth),
            'edge_source': edge_source,
            'edge_from': None if edge_from is None else int(edge_from),
            'edge_to': None if edge_to is None else int(edge_to),
        })
        return True

    def attach(self, results) -> None:
        """把依据附加到结果列表（就地修改）。无任何依据的节点记 0 条。"""
        for r in results:
            nid = int(r['id'])
            entries = self._entries.get(nid) or []
            r['supports'] = [dict(e) for e in entries]
            r['support_count'] = len(entries)
            r['support_truncated'] = nid in self._truncated
