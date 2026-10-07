"""Routing policy: try backends in order, accept an answer once it clears that step's
confidence bar, escalate the rest, and flag anything left over (or guarded) for a human."""
import hashlib
import json
import os
import threading
import time
import uuid

from .backends import BackendError, make_backend
from .schema import label_of, normalize_request

EXAMPLE_CONFIG = {
    "backends": {
        "local": {"type": "llm", "base_url": "http://127.0.0.1:8080/v1", "model": "local", "permutations": 2},
        "jev": {"type": "openrouter", "model": "typesafe/jev-1.13", "api_key_env": "OPENROUTER_API_KEY"},
    },
    "route": [
        {"backend": "local", "min_confidence": 0.85},
        {"backend": "jev", "min_confidence": 0.7},
    ],
    "on_low_confidence": "human",
    "guards": {"*": {"always_human": ["refund"]}},
    "audit_log": "decide-audit.jsonl",
    "review_queue": "decide-review.jsonl",
}


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = 0
        self.answers = 0
        self.decided_by = {}
        self.needs_human = 0
        self.errors = 0
        self.latencies = []

    def add(self, result):
        with self.lock:
            self.requests += 1
            self.latencies.append(result["latency_ms"])
            self.latencies = self.latencies[-5000:]
            for a in result["answers"].values():
                self.answers += 1
                by = a.get("decided_by") or "none"
                self.decided_by[by] = self.decided_by.get(by, 0) + 1
                self.needs_human += bool(a.get("needs_human"))
            self.errors += sum(1 for s in result["route"] if s.get("error"))

    def snapshot(self):
        with self.lock:
            lat = sorted(self.latencies)

            def pct(p):
                return lat[min(len(lat) - 1, int(p * len(lat)))] if lat else None

            return {
                "requests": self.requests,
                "answers": self.answers,
                "decided_by": dict(self.decided_by),
                "needs_human": self.needs_human,
                "needs_human_rate": round(self.needs_human / self.answers, 4) if self.answers else None,
                "backend_errors": self.errors,
                "latency_ms": {"p50": pct(0.5), "p95": pct(0.95)},
            }


class Gateway:
    def __init__(self, config, base_dir="."):
        self.config = config
        backends = config.get("backends") or {}
        if not backends:
            raise ValueError("config needs at least one backend")
        self.backends = {n: make_backend(n, c) for n, c in backends.items()}
        self.route = config.get("route") or [{"backend": next(iter(self.backends)), "min_confidence": 0.0}]
        for step in self.route:
            if step.get("backend") not in self.backends:
                raise ValueError(f"route step references unknown backend {step.get('backend')!r}")
        self.on_low = config.get("on_low_confidence", "human")
        if self.on_low not in ("human", "accept"):
            raise ValueError("on_low_confidence must be 'human' or 'accept'")
        self.guards = config.get("guards") or {}
        self.audit_path = self._path(config.get("audit_log"), base_dir)
        self.queue_path = self._path(config.get("review_queue"), base_dir)
        self.audit_state = bool(config.get("audit_include_state", False))
        self.stats = Stats()
        self.lock = threading.Lock()

    @staticmethod
    def _path(p, base_dir):
        if not p:
            return None
        return p if os.path.isabs(p) else os.path.join(base_dir, p)

    def _guarded(self, qid, answer):
        lab = label_of(answer)
        for key in (qid, "*"):
            g = self.guards.get(key) or {}
            if lab is not None and lab in [str(x) for x in g.get("always_human", [])]:
                return f"guard: {key}={lab} always goes to a human"
        return None

    def decide(self, request):
        t0 = time.perf_counter()
        state_text, norm = normalize_request(request)
        for qid in norm:
            norm[qid]["raw"] = request["questions"][qid]
        pending = dict(norm)
        attempts = {qid: [] for qid in norm}
        final, route_log = {}, []

        for i, step in enumerate(self.route):
            if not pending:
                break
            be = self.backends[step["backend"]]
            bar = float(step.get("min_confidence", 0.0))
            last = i == len(self.route) - 1
            s0 = time.perf_counter()
            entry = {"backend": be.name, "questions": list(pending)}
            try:
                answers = be.decide(state_text, request["state"], pending)
            except BackendError as e:
                entry["error"] = str(e)
                answers = {}
            entry["latency_ms"] = round((time.perf_counter() - s0) * 1000, 1)
            route_log.append(entry)
            for qid, a in answers.items():
                a = dict(a, backend=be.name)
                attempts[qid].append(a)
                conf = a.get("confidence")
                ok = conf is not None and conf >= bar
                if ok or (last and self.on_low == "accept"):
                    final[qid] = dict(a, decided_by=be.name, escalations=len(attempts[qid]) - 1,
                                      needs_human=False)
                    pending.pop(qid)

        for qid in pending:
            tried = attempts[qid]
            if tried:
                best = max(tried, key=lambda a: a.get("confidence") or 0)
                final[qid] = dict(best, decided_by=None, escalations=len(tried) - 1, needs_human=True,
                                  reason="low confidence on every backend")
            else:
                final[qid] = {"type": norm[qid]["type"], "decided_by": None, "needs_human": True,
                              "escalations": 0, "reason": "every backend failed"}
        for qid, a in final.items():
            g = self._guarded(qid, a) if not a["needs_human"] else None
            if g:
                a.update(needs_human=True, reason=g)

        result = {
            "id": "dec_" + uuid.uuid4().hex[:16],
            "answers": {qid: final[qid] for qid in norm},
            "needs_human": any(a["needs_human"] for a in final.values()),
            "route": route_log,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
        self.stats.add(result)
        self._log(request, state_text, result)
        return result

    def _append(self, path, obj):
        with self.lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    def _log(self, request, state_text, result):
        if self.audit_path:
            rec = {"ts": time.time(), "id": result["id"],
                   "state_sha256": hashlib.sha256(state_text.encode()).hexdigest(),
                   "questions": list(request["questions"]), "answers": result["answers"],
                   "route": result["route"], "latency_ms": result["latency_ms"]}
            if self.audit_state:
                rec["state"] = request["state"]
            self._append(self.audit_path, rec)
        if self.queue_path and result["needs_human"]:
            self._append(self.queue_path, {
                "ts": time.time(), "id": result["id"], "state": request["state"],
                "questions": {q: request["questions"][q] for q, a in result["answers"].items() if a["needs_human"]},
                "suggested": {q: a for q, a in result["answers"].items() if a["needs_human"]}})

    def stats_snapshot(self):
        return self.stats.snapshot()
