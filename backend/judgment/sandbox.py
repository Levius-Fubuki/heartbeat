"""文件工具路径白名单(Phase 6b)。read_file / list_dir / glob_files 调 is_allowed 守卫,越界返提示不抛。

安全模型:默认根是「地板」(~/Desktop、~/Documents、~/Downloads,始终放行),用户在设置页加的
allowed_dirs 是「叠加」(只能加不能减默认,安全可预测)。is_allowed 判断 path 是否落在任一根之下。

符号链接:expanduser+resolve 解析后比较 → 桌面里指向 /etc 的软链会被解析到白名单外,正确拒绝。
"""
from __future__ import annotations

import logging
from pathlib import Path

from backend.memory.settings import settings

log = logging.getLogger("heartbeat.judgment.sandbox")


def _default_roots() -> list:
    """默认地板根:桌面/文档/下载(始终放行,不可由设置移除)。"""
    home = Path.home()
    return [home / "Desktop", home / "Documents", home / "Downloads"]


def allowed_roots() -> list:
    """生效白名单根(默认根 ∪ settings.allowed_dirs),全部 expanduser+resolve 后返回(去重)。"""
    seen, roots = set(), []
    for r in _default_roots() + [Path(p) for p in (settings.allowed_dirs or []) if p]:
        try:
            rr = r.expanduser().resolve()          # 解析 ~ 与符号链接
        except Exception:  # noqa: BLE001
            continue
        if rr not in seen:
            seen.add(rr)
            roots.append(rr)
    return roots


def is_allowed(path) -> bool:
    """path 是否在任一白名单根之下(含根本身)。空/非法路径 → False。"""
    if not path:
        return False
    try:
        p = Path(path).expanduser().resolve()      # strict=False(3.6+ 默认):不存在路径不抛
    except Exception:  # noqa: BLE001
        return False
    for rr in allowed_roots():
        if p == rr or rr in p.parents:             # 落在根下或就是根本身
            return True
    return False
