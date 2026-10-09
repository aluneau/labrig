#!/usr/bin/env python3
"""Validate customer-case templates without a running app or libvirt (docs/templates.md).

    scripts/check-templates.py                 # backend/app/templates/*.yaml (+ DATA_DIR/templates)
    scripts/check-templates.py my-case.yaml    # specific files

Each file is parsed, its placeholders checked and rendered with sample parameters (every bool both ways)
through the GroupSpec / ClusterCreate schemas. Also renders each one with its defaults and a sample case
and prints the group name, subnet and estimate. Exit status 1 when a file is invalid.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services.template_service import coerce_params, template_service  # noqa: E402


def main() -> int:
    bad = 0
    if len(sys.argv) > 1:
        loaded = {}
        for name in sys.argv[1:]:
            path = Path(name)
            try:
                tpl = template_service.load_file(path)
                loaded[tpl.data.id] = tpl
            except Exception as e:  # noqa: BLE001 - report every file
                print(f"FAIL {path}: {e}")
                bad += 1
    else:
        loaded, errors = template_service.load()
        for err in errors:
            print(f"FAIL {err['file']}: {err['error']}")
            bad += 1
    for tid, tpl in sorted(loaded.items()):
        data = tpl.data
        sample = {"case": "12345678"}
        values, errs = coerce_params(data.params, sample)
        if errs:
            print(f"WARN {tid}: defaults + case=12345678: {'; '.join(errs)}")
            continue
        doc = template_service._render_doc(data, values, cidr_for_auto="10.99.0.0/24")
        if doc["errors"]:
            print(f"FAIL {tid}: {'; '.join(doc['errors'])}")
            bad += 1
            continue
        est = template_service.estimate(data, doc["group"], doc["cluster"])
        cluster = f", cluster {doc['cluster']['name']} ({doc['cluster'].get('type', 'k3s')})" if doc["cluster"] else ""
        print(f"ok   {tid}{' (custom)' if tpl.custom else ''}: group {doc['group']['name']}"
              f" {doc['group']['cidr']}, {len(doc['group'].get('members', []))} member(s){cluster};"
              f" ~{est.get('memory_mb', 0)} MiB, {est.get('vcpus', 0)} vCPU, {est.get('disk_gb', 0)} GiB")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
