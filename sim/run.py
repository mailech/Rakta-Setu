"""
CLI runner for a single negotiation.
Usage: python -m sim.run --bridge <bridge_id_or_index>
       python -m sim.run --bridge 0   (first bridge in list)
       python -m sim.run --bridge B017
       python -m sim.run --list       (show all bridge IDs)
"""
import asyncio
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from agents.orchestrator import init_db, run_negotiation
from agents.exchange import ExchangeAgent
from llm.wrapper import llm
from learning.lessons import get_playbook, save_playbook


def load_data():
    donors_path  = ROOT / "data" / "donors.json"
    bridges_path = ROOT / "data" / "bridges.json"
    config_path  = ROOT / "data" / "config.json"

    if not donors_path.exists():
        print("[run] donors.json not found — run `python -m data.engine` first")
        sys.exit(1)

    donors  = json.loads(donors_path.read_text(encoding="utf-8"))
    bridges = json.loads(bridges_path.read_text(encoding="utf-8"))
    config  = json.loads(config_path.read_text(encoding="utf-8"))
    return donors, bridges, config


async def run(bridge_identifier: str, use_llm: bool = False):
    donors, bridges, config = load_data()
    donors_by_id = {d["user_id"]: d for d in donors}

    # Find bridge
    bridge = None
    if bridge_identifier.isdigit():
        idx = int(bridge_identifier)
        if 0 <= idx < len(bridges):
            bridge = bridges[idx]
    else:
        for b in bridges:
            if bridge_identifier.lower() in b["bridge_id"].lower():
                bridge = b
                break

    if not bridge:
        print(f"[run] Bridge '{bridge_identifier}' not found.")
        print(f"[run] Available: 0..{len(bridges)-1} or partial bridge_id match")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"RAKTA-SETU Negotiation Simulation")
    print(f"{'='*60}")
    print(f"Bridge:       {bridge['bridge_id'][:20]}...")
    print(f"Blood Group:  {bridge.get('bridge_blood_group')}")
    print(f"Units Needed: {bridge.get('quantity_required')}")
    print(f"Date:         {bridge.get('next_transfusion_date')}")
    print(f"Roster Size:  {bridge.get('roster_size')}")
    print(f"LLM Mode:     {'LIVE (Bedrock)' if use_llm else 'MOCK'}")
    print(f"{'='*60}")

    init_db()
    exchange = ExchangeAgent(donors, bridges, config)
    llm_fn = llm if use_llm else None

    result = await run_negotiation(bridge, donors_by_id, exchange, config, llm_fn,
                                   auto_confirm=True)

    print(f"\n{'='*60}")
    print(f"RESULT: {result['result']}")
    print(f"Coverage: {result['coverage']}/{result['units_needed']} units")
    print(f"NEG ID:   {result['neg_id']}")
    print(f"{'='*60}")
    return result


async def replay(bridge_identifier: str, use_llm: bool = False):
    """
    Failure-learning demo (spec §6): run the same bridge twice.
      Run 1: Guardian opens late (low lead_days) -> donors decline on short notice.
      Lessons bump lead_days. Run 2: Guardian opens earlier -> donors accept.
    Prints the policy JSON diff — the 'system rewrote its own protocol' moment.
    """
    donors, bridges, config = load_data()
    donors_by_id = {d["user_id"]: d for d in donors}

    bridge = None
    if bridge_identifier.isdigit():
        idx = int(bridge_identifier)
        bridge = bridges[idx] if 0 <= idx < len(bridges) else None
    else:
        bridge = next((b for b in bridges
                       if bridge_identifier.lower() in b["bridge_id"].lower()), None)
    if not bridge:
        print(f"[replay] Bridge '{bridge_identifier}' not found.")
        sys.exit(1)

    bid = bridge["bridge_id"]
    init_db()
    llm_fn = llm if use_llm else None

    print(f"\n{'#'*60}\n# FAILURE-LEARNING REPLAY — {bridge['bridge_blood_group']} bridge\n{'#'*60}")

    # Force a 'short notice' starting condition so Run 1 fails on advance notice.
    save_playbook(bid, {"lead_days": 1, "overbook_factor": 1.5, "rotation_depth": 3})
    pb_before = get_playbook(bid, config)
    print(f"\n[RUN 1] Starting playbook: {pb_before}")
    ex1 = ExchangeAgent(donors, bridges, config)
    r1 = await run_negotiation(bridge, donors_by_id, ex1, config, llm_fn, auto_confirm=True)
    print(f"\n[RUN 1] -> {r1['result']} ({r1['coverage']}/{r1['units_needed']})")
    print(f"[RUN 1] Lessons: {r1.get('lessons', {}).get('summary')}")
    print(f"[RUN 1] Policy diff: {json.dumps(r1.get('policy_diff', {}).get('guardian', {}))}")

    pb_after = get_playbook(bid, config)
    print(f"\n[RUN 2] Guardian now opens {pb_after['lead_days']}d earlier "
          f"(was {pb_before['lead_days']}d). Re-running same bridge...")
    ex2 = ExchangeAgent(donors, bridges, config)
    r2 = await run_negotiation(bridge, donors_by_id, ex2, config, llm_fn, auto_confirm=True)
    print(f"\n[RUN 2] -> {r2['result']} ({r2['coverage']}/{r2['units_needed']})")

    print(f"\n{'#'*60}")
    print(f"# SELF-IMPROVEMENT: lead_days {pb_before['lead_days']} -> {pb_after['lead_days']}  "
          f"|  Run1={r1['result']}  Run2={r2['result']}")
    print(f"# Nobody told it to. The system rewrote its own protocol.")
    print(f"{'#'*60}")
    return {"run1": r1, "run2": r2, "playbook_before": pb_before, "playbook_after": pb_after}


def list_bridges():
    _, bridges, _ = load_data()
    print(f"\n{'='*60}")
    print(f"{'#':<4} {'Blood':<14} {'Units':<6} {'Days':<6} {'Health':<8} {'Roster'}")
    print(f"{'='*60}")
    for i, b in enumerate(bridges[:20]):
        print(f"{i:<4} {b.get('bridge_blood_group','?'):<14} {b.get('quantity_required',1):<6.0f} "
              f"{b.get('days_to_next_transfusion',999):<6} {b.get('health_label','?'):<8} {b.get('roster_size',0)}")
    if len(bridges) > 20:
        print(f"... and {len(bridges)-20} more")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAKTA-SETU CLI negotiation runner")
    parser.add_argument("--bridge", type=str, default="0", help="Bridge index or partial ID")
    parser.add_argument("--list",   action="store_true",  help="List all bridges")
    parser.add_argument("--llm",    action="store_true",  help="Use real LLM (Bedrock)")
    parser.add_argument("--replay", action="store_true",  help="Failure-learning replay demo (§6)")
    args = parser.parse_args()

    if args.list:
        list_bridges()
    elif args.replay:
        asyncio.run(replay(args.bridge, use_llm=args.llm))
    else:
        asyncio.run(run(args.bridge, use_llm=args.llm))
