"""robots.txt 解析与路径判定（见《项目开发说明_v1.md》9.4）。"""

from __future__ import annotations

import re
from dataclasses import dataclass

USER_AGENT_FIELD = "user-agent"
RULE_FIELDS = frozenset({"allow", "disallow"})


@dataclass(frozen=True)
class Rule:
    """一条路径规则；pattern 为 robots.txt 中的原始路径模式。"""

    allow: bool
    pattern: str


def _matches(pattern: str, path: str) -> bool:
    """按 RFC 9309 的前缀语义匹配，支持 `*` 通配与 `$` 结尾锚定。"""

    if not pattern:
        return False
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    expression = "^" + "".join(
        ".*" if char == "*" else re.escape(char) for char in body
    )
    if anchored:
        expression += "$"
    return re.search(expression, path) is not None


def _specificity(pattern: str) -> int:
    return len(pattern)


def _is_more_specific(candidate: Rule, current: Rule) -> bool:
    """候选规则是否比当前规则更具体；长度相同时 Allow 优先。"""

    candidate_length = _specificity(candidate.pattern)
    current_length = _specificity(current.pattern)
    if candidate_length != current_length:
        return candidate_length > current_length
    return candidate.allow and not current.allow


def _agent_token(user_agent: str) -> str:
    """取 User-Agent 的产物标识（`/` 之前的部分）。"""

    token = user_agent.split("/", 1)[0].strip().lower()
    return token or user_agent.strip().lower()


def parse_groups(text: str) -> list[tuple[list[str], list[Rule]]]:
    """把 robots.txt 解析为「User-Agent 组 → 规则列表」。"""

    groups: list[tuple[list[str], list[Rule]]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    has_rule = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == USER_AGENT_FIELD:
            if has_rule:
                groups.append((agents, rules))
                agents, rules, has_rule = [], [], False
            agents.append(value.lower())
            continue
        if field not in RULE_FIELDS or not agents:
            continue
        has_rule = True
        if field == "disallow" and not value:
            continue
        rules.append(Rule(allow=field == "allow", pattern=value))
    if agents:
        groups.append((agents, rules))
    return groups


def select_rules(text: str, user_agent: str) -> tuple[Rule, ...]:
    """选出与 User-Agent 匹配度最高的分组规则；无匹配时返回空规则。"""

    token = _agent_token(user_agent)
    best_specific = -1
    best: tuple[Rule, ...] = ()
    for agents, rules in parse_groups(text):
        for agent in agents:
            if agent == "*":
                if best_specific < 0:
                    best, best_specific = tuple(rules), 0
            elif agent and agent in token and len(agent) > best_specific:
                best, best_specific = tuple(rules), len(agent)
    return best


class RobotsPolicy:
    """针对某个 User-Agent 的 robots.txt 策略。"""

    def __init__(self, rules: tuple[Rule, ...]) -> None:
        self._rules = rules

    @classmethod
    def parse(cls, text: str, user_agent: str) -> RobotsPolicy:
        return cls(select_rules(text, user_agent))

    def allows(self, path: str) -> bool:
        """最长匹配优先；长度相同时 Allow 优先于 Disallow。"""

        target = path or "/"
        best: Rule | None = None
        for rule in self._rules:
            if not _matches(rule.pattern, target):
                continue
            if best is None or _is_more_specific(rule, best):
                best = rule
        return best is None or best.allow
