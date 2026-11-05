"""Summarize observed paired task timings only. Empty baselines stay unmeasured."""

import argparse
import csv
import json
import math
from pathlib import Path
import statistics


COLUMNS = ('pair_id', 'case_id', 'participant_id', 'order', 'manual_active_seconds',
    'assisted_active_seconds', 'manual_wall_seconds', 'assisted_wall_seconds',
    'manual_quality_pass', 'assisted_quality_pass', 'hourly_cost_usd', 'assisted_api_cost_usd', 'notes')


def summarize_timings(rows):
    observed, identifiers, empty = [], set(), 0
    for row in rows:
        if not any(str(row.get(key, '')).strip() for key in COLUMNS):
            empty += 1
            continue
        for key in ('pair_id', 'case_id', 'participant_id'):
            if not row.get(key, '').strip():
                raise ValueError(f'{key} is required for every observed pair')
        if row['pair_id'] in identifiers:
            raise ValueError('duplicate pair_id')
        identifiers.add(row['pair_id'])
        if row.get('order') not in {'manual_first', 'assisted_first'}:
            raise ValueError('order must record manual_first or assisted_first')
        record = dict(row)
        for key in ('manual_active_seconds', 'assisted_active_seconds', 'manual_wall_seconds', 'assisted_wall_seconds'):
            try:
                value = float(row[key])
            except (KeyError, ValueError, TypeError):
                raise ValueError(f'{key} needs an observed time, including unsuccessful attempts') from None
            if not math.isfinite(value) or value < 0:
                raise ValueError('time must be finite and nonnegative')
            record[key] = value
        for mode in ('manual', 'assisted'):
            if record[mode + '_active_seconds'] > record[mode + '_wall_seconds']:
                raise ValueError('active time cannot exceed wall time')
            if row.get(mode + '_quality_pass') not in {'true', 'false'}:
                raise ValueError('quality outcome must be true or false, including failures')
        for key in ('hourly_cost_usd', 'assisted_api_cost_usd'):
            value = row.get(key, '').strip()
            record[key] = float(value) if value else None
            if record[key] is not None and (not math.isfinite(record[key]) or record[key] < 0):
                raise ValueError('cost must be finite and nonnegative')
        observed.append(record)
    if not observed:
        return {'status': 'not_measured', 'pairs': 0, 'blank_rows': empty,
                'active_time_reduction_pct': None, 'net_cost_reduction_usd': None}
    manual = sum(r['manual_active_seconds'] for r in observed)
    assisted = sum(r['assisted_active_seconds'] for r in observed)
    paired = [r['manual_active_seconds'] - r['assisted_active_seconds'] for r in observed]
    matched = [r for r in observed if r['manual_quality_pass'] == r['assisted_quality_pass'] == 'true']
    measured_cost = all(r['hourly_cost_usd'] is not None and r['assisted_api_cost_usd'] is not None for r in observed)
    return {'status': 'measured_sample', 'pairs': len(observed), 'blank_rows': empty,
        'participants': len({r['participant_id'] for r in observed}), 'cases': len({r['case_id'] for r in observed}),
        'orders': {order: sum(r['order'] == order for r in observed) for order in ('manual_first', 'assisted_first')},
        'manual_quality_passes': sum(r['manual_quality_pass'] == 'true' for r in observed),
        'assisted_quality_passes': sum(r['assisted_quality_pass'] == 'true' for r in observed),
        'manual_active_seconds': manual, 'assisted_active_seconds': assisted,
        'active_time_reduction_pct': (manual - assisted) / manual * 100 if manual else None,
        'median_paired_active_seconds_saved': statistics.median(paired),
        'manual_median_wall_seconds': statistics.median(r['manual_wall_seconds'] for r in observed),
        'assisted_median_wall_seconds': statistics.median(r['assisted_wall_seconds'] for r in observed),
        'both_quality_pass_pairs': len(matched),
        'both_quality_pass_median_active_seconds_saved': statistics.median(
            r['manual_active_seconds'] - r['assisted_active_seconds'] for r in matched) if matched else None,
        'net_cost_reduction_usd': sum((r['manual_active_seconds'] - r['assisted_active_seconds']) / 3600
            * r['hourly_cost_usd'] - r['assisted_api_cost_usd'] for r in observed) if measured_cost else None,
        'limitations': ['실패한 작업의 측정 시간도 전체 비교에 포함했습니다.',
            '품질 통과율과 시간 절감률을 함께 제시해야 합니다.',
            '시간 감소는 이 표본의 작업 시간이며 실제 인력 감축이나 급여 절감을 입증하지 않습니다.',
            '같은 사례·참여자의 반복과 순서 효과 때문에 독립 표본의 일반화된 효율로 해석하지 않습니다.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with args.input.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or not set(COLUMNS) <= set(reader.fieldnames):
            raise ValueError('use the supplied measurement template')
        result = summarize_timings(reader)
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
