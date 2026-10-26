#!/usr/bin/env python3
"""One entry point: build, audit, freeze, run, report."""
import argparse
import json
from pathlib import Path
import builder
import runner


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('build', help='Generate current inputs and review pages offline')
    commands.add_parser('audit', help='Inspect structural checks and gaps without writing inputs')
    api_check = commands.add_parser('api-check', help='Read model lists separately for each credential; no completion requests')
    api_check.add_argument('--route', help='Inspect one named API route only')
    api_check.add_argument('--endpoints', action='store_true', help='Also inspect OpenRouter provider endpoints')
    freeze = commands.add_parser('freeze', help='Freeze current inputs/config/code into runs/')
    freeze.add_argument('--preflight', action='store_true', help='Select representative inputs, one repeat')
    freeze.add_argument('--preflight-profile', choices=['coverage'], help='One task, five subtypes at all three prices plus baseline; baseline probes first')
    freeze.add_argument('--models', nargs='+', help='Select model IDs, e.g. M03 for Claude')
    freeze.add_argument('--retry-parse-errors-from', type=Path, help='Preflight only: new requests for outputs still unparseable by the current parser')
    reparse = commands.add_parser('rescore', help='Offline reparse with provenance; never changes original results')
    reparse.add_argument('--run-dir', type=Path, required=True)
    reparse.add_argument('--stub', action='store_true')
    run = commands.add_parser('run', help='Execute or resume a frozen run')
    run.add_argument('--run-dir', type=Path, required=True)
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument('--stub', action='store_true', help='Offline pipeline check; no API calls')
    mode.add_argument('--live', action='store_true', help='Use configured provider; requires live_enabled and budget')
    run.add_argument('--max-new', type=int)
    report = commands.add_parser('report', help='Rebuild reports from saved results')
    report.add_argument('--run-dir', type=Path, required=True)
    report.add_argument('--stub', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'api-check':
        import transport
        result = transport.inspect_routes(builder.load(builder.DIRECTORY / 'config.json'), route=args.route, endpoints=args.endpoints)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'ok' else 1
    elif args.command == 'build':
        audit = builder.build()
        print(json.dumps({k: audit[k] for k in ['status', 'tasks', 'condition_counts', 'conditions', 'candidate_calls']}))
        print('Full readiness report:', builder.OUTPUT / 'audit.json')
    elif args.command == 'audit':
        cfg, tasks = builder.construct()
        design = builder.readiness(tasks)
        print(json.dumps({'structure': 'passed', 'matrix_ready': design['matrix_ready'],
                          'missing_task_subtype_pairs': len(design['missing_task_subtype_pairs']),
                          'incomplete_price_pairs': len(design['incomplete_price_pairs']),
                          'sources': builder.source_audit(), 'live_enabled': cfg['live_enabled']}, ensure_ascii=False, indent=2))
    elif args.command == 'freeze':
        print(runner.freeze(preflight=args.preflight, model_ids=args.models, parse_errors_from=args.retry_parse_errors_from, preflight_profile=args.preflight_profile))
    elif args.command == 'rescore':
        from rescore import rescore
        print(json.dumps(rescore(args.run_dir,stub=args.stub),ensure_ascii=False))
    else:
        if args.command == 'run' and args.max_new is not None and args.max_new < 1:
            parser.error('--max-new must be positive')
        result = runner.execute(args.run_dir, stub=args.stub,
                                max_new=getattr(args, 'max_new', None), report_only=args.command == 'report')
        if result['status'] in {'budget_limit', 'paused_api_errors'} or (result['complete'] and not result['all_valid']):
            return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
