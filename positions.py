"""FC온라인 spPosition 코드와 포메이션 시그니처 유틸.

코드는 넥슨 메타데이터(spposition.json) 기준이며, collector가 메타 동기화 때
실제 값과 다르면 경고를 출력한다.
"""

POS_NAME = {
    0: "GK", 1: "SW", 2: "RWB", 3: "RB", 4: "RCB", 5: "CB", 6: "LCB", 7: "LB", 8: "LWB",
    9: "RDM", 10: "CDM", 11: "LDM", 12: "RM", 13: "RCM", 14: "CM", 15: "LCM", 16: "LM",
    17: "RAM", 18: "CAM", 19: "LAM", 20: "RF", 21: "CF", 22: "LF", 23: "RW", 24: "RS",
    25: "ST", 26: "LS", 27: "LW", 28: "SUB",
}

_LINES = [
    range(1, 9),    # 수비
    range(9, 12),   # 수비형 미드
    range(12, 17),  # 중앙/측면 미드
    range(17, 20),  # 공격형 미드
    range(20, 28),  # 공격
]


def formation_sig(codes):
    """GK·교체를 제외한 선발 포지션 코드를 정렬해 문자열로. 포메이션의 고유 키."""
    return "-".join(str(c) for c in sorted(c for c in codes if c not in (0, 28)))


def formation_label(sig):
    """시그니처에서 '4-2-2-2' 같은 라인 표기를 역산."""
    codes = [int(x) for x in sig.split("-") if x]
    counts = [sum(1 for c in codes if c in line) for line in _LINES]
    parts = [counts[0]] + [n for n in counts[1:] if n]
    return "-".join(str(n) for n in parts)


def formation_positions(sig):
    return [POS_NAME[int(x)] for x in sig.split("-") if x]
