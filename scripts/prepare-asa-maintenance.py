#!/usr/bin/env python3
"""Prepare private backup storage and maintenance metadata; default is a preview."""
import argparse
import json
from pathlib import Path
from asa_ops.maintenance import prepare

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--replacement-receipt',type=Path)
    parser.add_argument('--supersede-operation')
    args=parser.parse_args()
    try:
        config=json.loads(args.config.read_text(encoding='utf-8'))['installation']
        print(json.dumps(prepare(config,apply=args.apply,replacement_receipt=args.replacement_receipt,operation_id=args.supersede_operation)))
    except Exception as error:
        print(json.dumps({'status':'blocked','code':getattr(error,'code',type(error).__name__)}))
        raise SystemExit(2)
