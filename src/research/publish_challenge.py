"""Attach a frozen, public replay to the dashboard's existing historical audit."""
import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from .signal_audit import write_json


def publish(selection_path, audit_path):
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    test = json.loads(selection_path.with_suffix(".test.json").read_text(encoding="utf-8"))
    if test["selection_sha256"]!=hashlib.sha256(selection_path.read_bytes()).hexdigest():
        raise ValueError("Test belongs to a different frozen selection")
    columns = ["entry_at_ms","symbol","direction","entry","stop_loss","take_profit","exit","exit_reason",
               "gross_return","fees","spread","slippage","funding","net_return","pnl"]
    try:
        fills = pd.read_csv(selection_path.with_suffix(".fills.csv"),usecols=columns)
    except pd.errors.EmptyDataError:
        fills = pd.DataFrame(columns=columns)
    if len(fills)!=test["stats"]["trades"] or abs(fills.pnl.sum()+10000-test["stats"]["final_equity"])>1e-6:
        raise ValueError("Historical journal does not reconcile to test equity")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["research_challenge"] = dict(
        period=test["period"],passed=test["passed"],selected=test["selected"],
        active_days=test["active_days"],symbols=test["symbols"],stats=test["stats"],
        double_costs=test["double_costs"],candidate_count=len(selection["candidates"]),
        selection_sha256=test["selection_sha256"],fills=fills[columns].to_dict("records"),
        comparison=[{**{k:c[k] for k in ("name","quantile","regime")},"limit_depth":c.get("depth"),
                     "periods":[{"trades":s["trades"],"return":s["net_equity_return_percent"]}
                                for s in c["validation"].values()]} for c in selection["candidates"]])
    tmp = audit_path.with_suffix(".json.tmp")
    write_json(tmp,audit)
    tmp.replace(audit_path)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection",type=Path)
    parser.add_argument("audit",type=Path)
    args = parser.parse_args()
    publish(args.selection,args.audit)
