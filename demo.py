"""AICKStudio recording demo. Python 3.10+, standard library only.

Only --live sends the selected synthetic case to TypeSafe. Local queue tasks
are simulated tool actions; there are no payment, claim, email, or ERP APIs.
"""
import argparse
import copy
import getpass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
POLICY_VERSION = "aick-demo-v1"
MODEL = "jev-1.13.0"
PRICE_PER_MILLION = 0.042  # USD list price checked 2026-09-28, estimate only.


def load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def questions():
    return load("questions.json")


def request_for(case):
    return {"model": MODEL, "state": case, "questions": questions()}


def number(value, lo=0, hi=1):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a number")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError("Number is outside the expected range")
    return value


def validate(response):
    """Reject unusable responses before our policy can use them."""
    if not isinstance(response.get("model"), str):
        raise ValueError("Missing model ID")
    a = response["answers"]
    route, risk, urgency = a["route"], a["needs_review"], a["urgency"]
    if [route["type"], risk["type"], urgency["type"]] != ["choice", "noul", "score"]:
        raise ValueError("Answer types differ from questions")
    allowed = set(questions()["route"]["criteria"])
    if route["choice"] not in allowed or set(route["probabilities"]) != allowed:
        raise ValueError("Route not in allowlist")
    for answer, keys in [(route, allowed), (urgency, {"0", "1", "2"})]:
        if set(answer["probabilities"]) != keys:
            raise ValueError("Incomplete probability distribution")
        vals = [number(x) for x in answer["probabilities"].values()]
        if abs(sum(vals) - 1) > 0.02:
            raise ValueError("Probabilities do not sum to one")
        number(answer["confidence"])
    number(risk["noul"])
    number(urgency["score"], 0, 2)
    if route["probabilities"][route["choice"]] + 1e-6 < max(route["probabilities"].values()):
        raise ValueError("Chosen option is not a maximum")
    return a


def fixture(route="invoice_review", confidence=0.97, review=0.03):
    """Hand-authored branch fixture, never claimed to be Jev inference."""
    options = list(questions()["route"]["criteria"])
    p = {x: (0.96 if x == route else 0.04 / (len(options) - 1)) for x in options}
    return {"model": "FIXTURE-NOT-JEV", "answers": {
        "route": {"type": "choice", "choice": route, "probabilities": p, "confidence": confidence},
        "needs_review": {"type": "noul", "noul": review},
        "urgency": {"type": "score", "score": 0.3, "confidence": 0.75,
                    "probabilities": {"0": 0.7, "1": 0.3, "2": 0.0},
                    "legend": {"0": "Routine", "1": "Time-sensitive", "2": "Immediate review"}}
    }}


def outcome(queue, code, detail):
    return {"queue": queue, "policy_code": code, "reason": detail,
            "action": "create_local_review_task", "external_action": "NONE"}


def decide(case, response, error=None):
    # POLICY START: authenticated metadata precedes semantic recommendations.
    t = case.get("trusted", {})
    if t.get("bank_details_changed") is True:
        return outcome("vendor_verification", "BANK_CHANGE",
                       "Verify new bank details using a known contact; reviewer required.")
    amount = t.get("amount_usd")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool) and amount >= 25000:
        return outcome("finance_senior_review", "AMOUNT_LIMIT",
                       "Synthetic demo policy: amounts of USD 25,000 or more need senior review.")
    if t.get("domain") == "insurance":
        docs = t.get("documents_received", [])
        missing = sorted({"claim_form", "repair_estimate"} - set(docs))
        if missing:
            return outcome("claims_document_review", "MISSING_DOCUMENTS",
                           "Demo checklist missing: " + ", ".join(missing))
    if error:
        return outcome("manual_review", "API_UNAVAILABLE", error)
    try:
        a = validate(response)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return outcome("manual_review", "INVALID_RESPONSE", type(exc).__name__)
    if a["route"]["confidence"] < 0.85:
        return outcome("manual_review", "LOW_CONFIDENCE", "Demo route-confidence floor: 0.85.")
    if a["needs_review"]["noul"] >= 0.20:
        return outcome("manual_review", "REVIEW_SIGNAL", "Demo additional-review probability threshold: 0.20.")
    route = a["route"]["choice"]
    if route == "other":
        return outcome("manual_review", "OTHER_ROUTE", "No clear supported workflow.")
    domain_routes = {
        "finance": {"invoice_review", "vendor_change_review"},
        "insurance": {"claim_intake_review", "claim_document_review"}
    }
    if route not in domain_routes.get(t.get("domain"), set()):
        return outcome("manual_review", "DOMAIN_MISMATCH", "Recommendation conflicts with trusted domain.")
    queues = {"invoice_review": "finance_invoice_review",
              "vendor_change_review": "vendor_verification",
              "claim_intake_review": "claims_intake_review",
              "claim_document_review": "claims_document_review"}
    return outcome(queues[route], "ALLOWED_ROUTE", "Passed demo rules; queued for staff review.")


def call_live(payload, api_key):
    request = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}, method="POST")
    start = time.perf_counter()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=15, context=ssl.create_default_context()) as r:
                body = json.loads(r.read().decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("Expected an object response")
            return body, round((time.perf_counter() - start) * 1000, 1), None
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < 2:
                raw = e.headers.get("Retry-After", "")
                delay = float(raw) if raw.isdigit() else 2 ** attempt
                if delay > 10:
                    return None, round((time.perf_counter() - start) * 1000, 1), "Server asks for a longer retry delay; rerun later."
                time.sleep(delay)
                continue
            return None, round((time.perf_counter() - start) * 1000, 1), f"HTTP {e.code}; check account, credits, model and request."
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError) as e:
            return None, round((time.perf_counter() - start) * 1000, 1), f"Transport/JSON failure ({type(e).__name__}); manual review."
    return None, None, "Retry budget exhausted."


def write_result(case, response, mode, elapsed, error):
    out = BASE / "out"
    out.mkdir(exist_ok=True)
    payload = request_for(case)
    decision = decide(case, response, error)
    usage = (response or {}).get("usage", {})
    if not isinstance(usage, dict):
        usage = {}
    tokens = usage.get("input_tokens")
    cost = tokens * PRICE_PER_MILLION / 1000000 if isinstance(tokens, int) and mode == "LIVE API" else None
    event = {"run_id": str(uuid.uuid4()), "time_utc": datetime.now(timezone.utc).isoformat(),
        "case_id": case["id"], "mode": mode, "policy_version": POLICY_VERSION,
        "request_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        "requested_model": MODEL, "returned_model": (response or {}).get("model"),
        "api_ms_including_retries": elapsed, "input_tokens": tokens,
        "estimated_api_usd": cost, "answers": (response or {}).get("answers"),
        "error": error, "decision": decision}
    stem = f'{case["id"]}_{event["run_id"][:8]}'
    (out / (stem + "_request.json")).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out / (stem + "_response.json")).write_text(json.dumps(response, indent=2), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(event, indent=2), encoding="utf-8")
    with (out / "audit.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    with (out / "tasks.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run_id": event["run_id"], "case_id": case["id"], **decision}) + "\n")
    report = dashboard(case, event)
    (out / "report.html").write_text(report, encoding="utf-8")
    (out / (stem + ".html")).write_text(report, encoding="utf-8")
    print(json.dumps(event, indent=2))
    print("\nOpen out/report.html. Each run also saves a unique HTML snapshot.")
    return event


def dashboard(case, event):
    e = html.escape
    try:
        a = validate({"model": event.get("returned_model"), "answers": event.get("answers")})
    except (ValueError, KeyError, TypeError, AttributeError):
        a = {}
    route = a.get("route", {})
    risk = a.get("needs_review", {})
    urgency = a.get("urgency", {})
    d = event["decision"]
    mode = event["mode"]
    bars = "".join(f'<div class="barlabel">{e(k)} <b>{v:.3f}</b></div><div class="track"><i style="width:{max(0,min(v,1))*100}%"></i></div>' for k,v in route.get("probabilities",{}).items() if isinstance(v,(int,float)))
    def display(v):
        return "Not available" if v is None else e(str(v))
    return f'''<!doctype html><html><head><meta charset="utf-8"><title>AICKStudio | Decision Desk</title>
<style>*{{box-sizing:border-box}}body{{margin:0;background:#0c1424;color:#e7edf7;font:18px/1.5 Arial,sans-serif}}main{{max-width:1400px;margin:auto;padding:32px}}header{{display:flex;justify-content:space-between;align-items:center}}h1{{font-size:40px;margin:6px 0}}h2{{font-size:19px;color:#8babc9;text-transform:uppercase;letter-spacing:1px}}.tag{{background:#254c63;color:#8ff1d8;padding:10px 14px;border-radius:6px}}.grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:18px;margin:24px 0}}.card{{background:#152237;border:1px solid #31445f;border-radius:10px;padding:22px;overflow-wrap:anywhere}}.big{{font-size:26px;font-weight:bold;color:#76dec8}}.amber{{color:#ffc781}}.small{{font-size:14px;color:#a9bbd0}}pre{{white-space:pre-wrap;font:15px/1.5 Consolas,monospace}}.track{{height:8px;background:#2b3e57;margin-bottom:13px;border-radius:8px}}i{{display:block;height:8px;background:#62d3bb}}.barlabel{{font-size:14px;display:flex;justify-content:space-between}}details{{margin-top:18px}}.metrics{{display:flex;gap:34px;flex-wrap:wrap}}p{{margin:10px 0}}@media(max-width:950px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><main>
<header><div><div class="small">AICKSTUDIO / ENTERPRISE AI LAB</div><h1>Who gets the next task?</h1></div><div class="tag">{e(mode)}</div></header>
<p>Synthetic data. Local review tasks only. No money moved. No claim approved or denied.</p>
<div class="grid"><section class="card"><h2>01 / Case evidence</h2><div class="big">{e(case['id'])}</div><p>{e(case['message'])}</p><details open><summary>Trusted demo metadata</summary><pre>{e(json.dumps(case['trusted'],indent=2))}</pre></details></section>
<section class="card"><h2>02 / Model recommendation</h2><div class="big">{e(route.get('choice','No usable answer'))}</div><p>Route confidence: <b>{display(route.get('confidence'))}</b></p>{bars}<p>Additional-review probability: <b>{display(risk.get('noul'))}</b></p><p>Urgency score (0-2): <b>{display(urgency.get('score'))}</b></p><p class="small">Confidence is not the selected option's probability. Urgency is display-only in this demo.</p></section>
<section class="card"><h2>03 / Code-controlled action</h2><div class="big amber">{e(d['queue'])}</div><p><b>{e(d['policy_code'])}</b></p><p>{e(d['reason'])}</p><p>Tool: create_local_review_task</p><p>External action: <b>NONE</b></p></section></div>
<div class="card metrics"><div>Returned model<br><b>{display(event['returned_model'])}</b></div><div>API wall time incl. retries<br><b>{display(event['api_ms_including_retries'])} ms</b></div><div>Input tokens<br><b>{display(event['input_tokens'])}</b></div><div>Estimated API cost (USD)<br><b>{display(event['estimated_api_usd'])}</b></div></div>
<p class="small">Run {e(event['run_id'])} | {e(event['time_utc'])} | Policy {e(POLICY_VERSION)} | Estimates use $0.042/M input tokens, checked 2026-09-28.</p>
<details><summary>Audit record / policy reason (written by code)</summary><pre>{e(json.dumps(event,indent=2))}</pre></details></main></body></html>'''


def selftest():
    cases = load("cases.json")
    runs = [
      ("routine invoice", cases["invoice"], fixture(), None, "ALLOWED_ROUTE"),
      ("bank change overrides high confidence", cases["bank-change"], fixture(confidence=0.99, review=0.01), None, "BANK_CHANGE"),
      ("missing docs override recommendation", cases["claim-missing"], fixture("claim_intake_review"), None, "MISSING_DOCUMENTS"),
      ("complete claim intake", cases["claim-complete"], fixture("claim_intake_review"), None, "ALLOWED_ROUTE"),
      ("uncertain routing", cases["invoice"], fixture(confidence=0.4), None, "LOW_CONFIDENCE"),
      ("review probability", cases["invoice"], fixture(review=0.7), None, "REVIEW_SIGNAL"),
      ("API failure", cases["invoice"], None, "Simulated timeout", "API_UNAVAILABLE"),
      ("invalid response", cases["invoice"], {}, None, "INVALID_RESPONSE"),
      ("domain mismatch", cases["invoice"], fixture("claim_intake_review"), None, "DOMAIN_MISMATCH"),
      ("other route", cases["invoice"], fixture("other"), None, "OTHER_ROUTE"),
      ("injection cannot alter trusted flag", cases["injection"], fixture(), None, "BANK_CHANGE"),
    ]
    large = copy.deepcopy(cases["invoice"])
    large["trusted"]["amount_usd"] = 25000
    runs.append(("amount boundary", large, fixture(), None, "AMOUNT_LIMIT"))
    for title, case, response, error, expected in runs:
        actual = decide(case, response, error)
        assert actual["policy_code"] == expected, (title, actual)
        assert actual["external_action"] == "NONE"
        print("PASS | " + title + " | " + expected)
    print(f"\n{len(runs)} local policy checks passed. These are NOT Jev accuracy tests.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", nargs="?", default="invoice", choices=list(load("cases.json")))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true")
    modes.add_argument("--fixture", choices=["normal", "overconfident", "uncertain", "timeout", "invalid"])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        selftest()
        return
    case = load("cases.json")[args.case]
    if args.live:
        key = os.environ.get("TYPESAFE_API_KEY") or getpass.getpass("Paste TypeSafe API key (hidden input): ")
        if not key.strip():
            raise SystemExit("No key entered. No API request made.")
        response, elapsed, error = call_live(request_for(case), key.strip())
        write_result(case, response, "LIVE API" if response is not None else "LIVE API ERROR", elapsed, error)
    else:
        scenario = args.fixture or "normal"
        route = "claim_intake_review" if case["trusted"].get("domain") == "insurance" else "invoice_review"
        response = fixture(route)
        error = None
        if scenario == "overconfident":
            response = fixture("invoice_review", confidence=0.99, review=0.01)
        elif scenario == "uncertain":
            response = fixture(route, confidence=0.4)
        elif scenario == "timeout":
            response, error = None, "SIMULATED timeout; no network call made."
        elif scenario == "invalid":
            response = {}
        write_result(case, response, "POLICY TEST / FIXTURE - NOT JEV", None, error)


if __name__ == "__main__":
    main()
