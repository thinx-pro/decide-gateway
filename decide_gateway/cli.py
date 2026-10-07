"""decide: one /v1/decide endpoint for decision models, with confidence escalation and a red-team suite."""
import argparse
import json
import os
import sys

from . import __version__, redteam
from .backends import BackendError, make_backend
from .gateway import EXAMPLE_CONFIG, Gateway
from .schema import SchemaError
from .server import serve


def _load_config(args):
    if getattr(args, "llm", None):
        be = {"type": "llm", "base_url": args.llm, "model": args.model or "local",
              "permutations": args.permutations if args.permutations is not None else 2}
        if args.api_key_env:
            be["api_key_env"] = args.api_key_env
        if getattr(args, "unhardened", False):
            be["harden"] = False
        return {"backends": {"llm": be}, "route": [{"backend": "llm", "min_confidence": args.min_confidence}]}, "."
    if getattr(args, "mock", False):
        return {"backends": {"mock": {"type": "mock"}},
                "route": [{"backend": "mock", "min_confidence": args.min_confidence}]}, "."
    path = args.config or ("decide.json" if os.path.exists("decide.json") else None)
    if not path:
        sys.exit("no backend: pass --config decide.json (see `decide init`), --llm URL, or --mock")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    for be in cfg.get("backends", {}).values():
        if be.get("type") == "llm":
            if args.permutations is not None:
                be["permutations"] = args.permutations
            if getattr(args, "unhardened", False):
                be["harden"] = False
    return cfg, os.path.dirname(os.path.abspath(path))


def _parse_q(spec, kind):
    """'id: question = a, b, c' (choice/score) or 'id: question' (yesno)."""
    if ":" not in spec:
        sys.exit(f"bad --{kind} {spec!r}: expected 'id: question{' = a, b, c' if kind != 'yesno' else ''}'")
    qid, rest = spec.split(":", 1)
    if kind == "yesno":
        return qid.strip(), {"type": "noul", "instructions": rest.strip()}
    if "=" not in rest:
        sys.exit(f"bad --{kind} {spec!r}: expected 'id: question = a, b, c'")
    instr, opts = rest.split("=", 1)
    opts = [o.strip() for o in opts.split(",") if o.strip()]
    if kind == "choice":
        return qid.strip(), {"type": "choice", "instructions": instr.strip(), "criteria": {o: "" for o in opts}}
    return qid.strip(), {"type": "score", "instructions": instr.strip(), "criteria": opts}


def _read(x):
    if x == "-":
        return sys.stdin.read()
    if x.startswith("@"):
        with open(x[1:], encoding="utf-8") as f:
            return f.read()
    return x


def cmd_init(args):
    if os.path.exists(args.path) and not args.force:
        sys.exit(f"{args.path} exists (use --force to overwrite)")
    with open(args.path, "w", encoding="utf-8") as f:
        json.dump(EXAMPLE_CONFIG, f, indent=2)
        f.write("\n")
    print(f"wrote {args.path}; edit the backends, then: decide serve -c {args.path}")


def cmd_ask(args):
    cfg, base = _load_config(args)
    gw = Gateway(cfg, base)
    if args.request:
        req = json.loads(_read(args.request))
    else:
        if not args.state:
            sys.exit("pass a request JSON file, or --state with --choice/--score/--yesno")
        qs = {}
        for kind in ("choice", "score", "yesno"):
            for spec in getattr(args, kind) or []:
                qid, q = _parse_q(spec, kind)
                qs[qid] = q
        if not qs:
            sys.exit("add at least one --choice, --score or --yesno question")
        req = {"state": _read(args.state), "questions": qs}
    try:
        res = gw.decide(req)
    except SchemaError as e:
        sys.exit(f"invalid request: {e}")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 2 if res["needs_human"] and args.exit_code else 0


def cmd_serve(args):
    cfg, base = _load_config(args)
    gw = Gateway(cfg, base)
    httpd = serve(gw, args.host, args.port, args.quiet)
    route = " -> ".join(f"{s['backend']}(>={s.get('min_confidence', 0)})" for s in gw.route)
    print(f"decide-gateway {__version__} on http://{args.host}:{args.port}/v1/decide  route: {route} -> {gw.on_low}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_redteam(args):
    cfg, _ = _load_config(args)
    name = args.backend or (cfg.get("route") or [{}])[0].get("backend") or next(iter(cfg["backends"]))
    if name not in cfg["backends"]:
        sys.exit(f"unknown backend {name!r}; have {list(cfg['backends'])}")
    be = make_backend(name, cfg["backends"][name])
    suite = redteam.load_suite(args.cases)
    color = sys.stdout.isatty() and not args.no_color
    if not args.json:
        print(f"running {len(suite['cases'])} cases against {name} ...", file=sys.stderr, flush=True)
    try:
        report = redteam.run(be, suite, order_test=not args.no_order_test)
    except BackendError as e:
        sys.exit(str(e))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(redteam.render(report, color))
    s = report["summary"]
    if args.max_injection is not None and (s["injection_success"] or 0) > args.max_injection:
        return 1
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="decide", description=__doc__)
    p.add_argument("--version", action="version", version=f"decide-gateway {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def backend_args(sp):
        g = sp.add_argument_group("backend (pick one)")
        g.add_argument("-c", "--config", help="gateway config JSON (default: ./decide.json if present)")
        g.add_argument("--llm", metavar="URL", help="ad-hoc OpenAI-compatible endpoint with logprobs, e.g. http://127.0.0.1:8080/v1")
        g.add_argument("--model", help="model name for --llm")
        g.add_argument("--api-key-env", help="env var holding the API key for --llm")
        g.add_argument("--mock", action="store_true", help="offline keyword backend (for wiring/tests)")
        g.add_argument("--min-confidence", type=float, default=0.0, help="confidence bar for --llm/--mock (default 0)")
        g.add_argument("--permutations", type=int, help="option orders averaged per question by llm backends (default 2)")

    sp = sub.add_parser("init", help="write an example decide.json")
    sp.add_argument("path", nargs="?", default="decide.json")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("ask", help="answer one request and print the JSON result")
    backend_args(sp)
    sp.add_argument("request", nargs="?", help="request JSON: inline, @file or - for stdin")
    sp.add_argument("--state", help="state text, @file or -")
    sp.add_argument("--choice", action="append", metavar="'id: question = a, b, c'")
    sp.add_argument("--score", action="append", metavar="'id: question = low, mid, high'")
    sp.add_argument("--yesno", action="append", metavar="'id: question'")
    sp.add_argument("--exit-code", action="store_true", help="exit 2 when a human is needed")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("serve", help="run the HTTP gateway (POST /v1/decide)")
    backend_args(sp)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8787)
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_serve)

    sp = sub.add_parser("redteam", help="run the injection / order / calibration suite against one backend")
    backend_args(sp)
    sp.add_argument("--backend", help="backend name from the config (default: first in route)")
    sp.add_argument("--cases", help="custom cases JSON (same format as the built-in suite)")
    sp.add_argument("--unhardened", action="store_true", help="llm backends: drop the untrusted-state system prompt")
    sp.add_argument("--no-order-test", action="store_true", help="skip the reversed-options pass")
    sp.add_argument("--max-injection", type=float, help="exit 1 if injection success rate exceeds this (for CI)")
    sp.add_argument("--json", action="store_true", help="print the full report as JSON")
    sp.add_argument("--out", help="also write the JSON report here")
    sp.add_argument("--no-color", action="store_true")
    sp.set_defaults(fn=cmd_redteam)

    args = p.parse_args(argv)
    try:
        return args.fn(args) or 0
    except ValueError as e:
        sys.exit(f"config error: {e}")


if __name__ == "__main__":
    sys.exit(main())
