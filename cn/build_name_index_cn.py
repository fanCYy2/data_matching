# -*- coding: utf-8 -*-
"""一次性构建【预归一化名字索引】name_index_cn.parquet(authorid, kind, nn, ws)。

动机:cn_match 每轮的两处慢点都是对 1 亿行全表现算归一化——
  · exact:扫 sciscinet_authors.parquet,逐行 norm(display_name)   (~247s)
  · alias:扫 sciscinet_author_details.parquet,from_json+unnest→1.39 亿条别名,再 norm+wset (~371s)
这两步每次跑都重来。把结果物化一次,以后 cn_match 只需拿 ~4.5 万 CN 变体键去 join 这张
预归一化索引(纯等值哈希连接),秒级完成。

索引口径(与 cn_match 的 norm/wset 宏一致,故直接 import 复用同一 con,避免口径漂移):
  · kind='e':来自 authors.display_name。exact 只按 nn 匹配 → 只填 nn,ws 置 NULL。
  · kind='a':来自 author_details 的 display_name_alternatives 逐个别名。alias 按 nn 或 ws 匹配
              → nn、ws 都填。

用法:  cd d:\\data_matching\\cn  &&  python build_name_index_cn.py
约 10 分钟、产出单个 parquet(几 GB)。建好后 cn_match.py 自动走索引路径。
"""
import time
import cn_match as M            # 复用 con 与 norm/wset 宏(import 时 name_index 尚不存在,USE_INDEX=False,无副作用)
con = M.con

OUT = "name_index_cn.parquet"
t0 = time.time()
print("开始构建 %s …(扫 authors + author_details 各 1 亿行,预计 ~10 分钟)" % OUT, flush=True)

con.sql(f"""
    COPY (
        -- exact 面:作者规范显示名
        SELECT authorid, 'e' AS kind,
               norm(display_name) AS nn, CAST(NULL AS VARCHAR) AS ws
        FROM '../sciscinet_authors.parquet'
        WHERE display_name IS NOT NULL AND trim(display_name) <> ''
        UNION ALL
        -- alias 面:每个别名展开一行,nn 与 ws 都算好
        SELECT authorid, 'a' AS kind,
               norm(alt) AS nn, wset(alt) AS ws
        FROM (
            SELECT authorid,
                   unnest(from_json(display_name_alternatives, '["VARCHAR"]')) AS alt
            FROM '../sciscinet_author_details.parquet'
        )
        WHERE alt IS NOT NULL AND trim(alt) <> ''
    ) TO '{OUT}' (FORMAT parquet)
""")

n  = con.sql(f"SELECT count(*) FROM '{OUT}'").fetchone()[0]
ne = con.sql(f"SELECT count(*) FROM '{OUT}' WHERE kind='e'").fetchone()[0]
na = con.sql(f"SELECT count(*) FROM '{OUT}' WHERE kind='a'").fetchone()[0]
print("完成:%s 共 %d 行(exact %d / alias %d),耗时 %.0fs。"
      % (OUT, n, ne, na, time.time() - t0), flush=True)
print("之后 cn_match.py 会自动走索引(CN_INDEX=0 可强制回退现扫)。", flush=True)
