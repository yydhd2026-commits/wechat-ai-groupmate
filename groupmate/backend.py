# -*- coding: utf-8 -*-
"""收发后端抽象。

把「怎么从微信拿消息 / 怎么发消息」隔离出来,好处有两个:
  1. 核心决策逻辑可以离线测试(用 MockBackend);
  2. 哪天 wechatauto-replica 在某个微信版本上失效,只换这一层。
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional, Protocol

from .message import GroupMessage


class Backend(Protocol):
    """后端必须实现的最小接口。"""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def poll(self) -> List[GroupMessage]:
        """取回新消息(增量)。无新消息返回空列表。"""
        ...

    def send(self, group: str, text: str) -> bool:
        """把文本发到指定群,返回是否成功。"""
        ...

    def recent(self, group: str, limit: int = 15) -> List[GroupMessage]:
        """取该群最近的消息,用于给模型提供上下文。"""
        ...


class MockBackend:
    """离线演示/测试用后端:喂脚本化消息,记录所有"已发送"的内容。"""

    def __init__(self, script: Optional[List[GroupMessage]] = None,
                 fail_sends: bool = False):
        self.script: List[GroupMessage] = list(script or [])
        self.sent: List[tuple] = []
        self.fail_sends = fail_sends
        self._cursor = 0
        self._all: List[GroupMessage] = []
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def feed(self, msg: GroupMessage) -> None:
        self.script.append(msg)

    def poll(self) -> List[GroupMessage]:
        out = self.script[self._cursor:]
        self._cursor = len(self.script)
        self._all.extend(out)
        return out

    def send(self, group: str, text: str) -> bool:
        if self.fail_sends:
            return False
        self.sent.append((group, text, time.time()))
        self._all.append(GroupMessage(group=group, sender="我", content=text,
                                      is_self=True, timestamp=time.time()))
        return True

    def recent(self, group: str, limit: int = 15) -> List[GroupMessage]:
        return [m for m in self._all if m.group == group][-limit:]
