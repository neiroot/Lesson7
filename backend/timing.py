from collections import defaultdict


def normalize_intervals(intervals, length):
    result = []
    for label, start, end in intervals:
        start, end = max(0, round(start)), min(length, round(end))
        if end > start:
            result.append((label, start, end))
    return sorted(set(result), key=lambda item: (item[1], item[2], item[0]))


def match_speakers_at_boundary(previous, current):
    """Match labels only when both blocks agree on who spoke at the same times."""
    shared = defaultdict(lambda: defaultdict(int))
    reverse = defaultdict(lambda: defaultdict(int))
    left = right = 0
    while left < len(previous) and right < len(current):
        old, old_start, old_end = previous[left]
        new, new_start, new_end = current[right]
        overlap = min(old_end, new_end) - max(old_start, new_start)
        if overlap > 0:
            shared[new][old] += overlap
            reverse[old][new] += overlap
        if old_end <= new_end:
            left += 1
        if new_end <= old_end:
            right += 1

    matches = {}
    for new, scores in shared.items():
        old = max(scores, key=scores.get)
        overlap = scores[old]
        if overlap >= 500 and 3 * overlap >= 2 * max(sum(scores.values()), sum(reverse[old].values())):
            matches[new] = old
    return matches


def timeline(regular, exclusive):
    events = defaultdict(list)
    for kind, intervals in enumerate((regular, exclusive)):
        for label, start, end in intervals:
            events[start].append((kind, label, 1))
            events[end].append((kind, label, -1))
    active = [{}, {}]
    previous = None
    for point, changes in sorted(events.items()):
        if previous is not None and any(active):
            normal, sole = map(set, active)
            overlap = len(normal) > 1 or len(sole) > 1
            consistent = not normal or normal == sole
            label = next(iter(sole)) if len(sole) == 1 and not overlap and consistent else None
            yield previous, point, label, overlap, normal
        for kind, label, delta in changes:
            count = active[kind].get(label, 0) + delta
            if count:
                active[kind][label] = count
            else:
                active[kind].pop(label, None)
        previous = point


def asr_windows(spans):
    grouped = []
    for start, end, label, overlap, _ in spans:
        if grouped and (label, overlap) == grouped[-1][2:] and (
            start == grouped[-1][1] or (label is not None and start - grouped[-1][1] <= 300)
        ):
            grouped[-1] = (grouped[-1][0], end, label, overlap)
        else:
            grouped.append((start, end, label, overlap))
    for start, end, label, overlap in grouped:
        # Balanced windows avoid a near-empty tail on long turns.
        count = (end - start + 19999) // 20000
        for i in range(count):
            yield (start + (end - start) * i // count, start + (end - start) * (i + 1) // count, label, overlap)


def clean_windows(spans, label):
    windows = []
    for start, end, _, overlap, normal in spans:
        if overlap or normal != {label}:
            continue
        if windows and windows[-1][1] == start:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return windows


def review_windows(spans, regular, label):
    candidates = []
    for start, end in clean_windows(spans, label):
        while end - start >= 25000 and len(candidates) < 3:
            candidates.append((start, start + 20000, True))
            start += 20000
        if end > start:
            candidates.append((start, min(end, start + 20000), True))
    if not candidates:
        candidates = [(start, min(end, start + 20000), False)
                      for speaker, start, end in regular if speaker == label]
    selected = []
    for candidate in sorted(candidates, key=lambda item: (-(item[1] - item[0]), item[0])):
        if all(candidate[1] <= start or candidate[0] >= end for start, end, _ in selected):
            selected.append(candidate)
        if len(selected) == 3:
            break
    return selected
