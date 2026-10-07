"""Request validation and answer normalisation for the System One style schema.

A request is ``{"state": ..., "questions": {id: question}}`` where each question is
one of three types (the shape shared by TypeSafe Jev, OpenRouter's
/api/alpha/decisions and Laya):

* ``choice``: ``criteria`` is ``{label: description}`` (a plain list of labels is accepted too)
* ``score``:  ``criteria`` is an ordered list of level descriptions, lowest first
* ``noul``:   yes/no; ``criteria`` is optional ``{"true": ..., "false": ...}``
"""
import json

TYPES = ("choice", "score", "noul")


class SchemaError(ValueError):
    pass


def _text(x):
    if isinstance(x, str):
        return x
    return json.dumps(x, ensure_ascii=False, indent=1)


def state_to_text(state):
    if state is None or state == "" or state == {} or state == []:
        raise SchemaError("'state' is required")
    return _text(state)


def normalize_question(qid, q):
    """Return {"type", "instructions", "options": [(label, description), ...]}."""
    if not isinstance(q, dict):
        raise SchemaError(f"question {qid!r} must be an object")
    t = q.get("type")
    if t not in TYPES:
        raise SchemaError(f"question {qid!r}: type must be one of {TYPES}, got {t!r}")
    instr = q.get("instructions")
    if not instr:
        raise SchemaError(f"question {qid!r}: 'instructions' is required")
    crit = q.get("criteria")
    if t == "choice":
        if isinstance(crit, list):
            crit = {str(c): "" for c in crit}
        if not isinstance(crit, dict) or len(crit) < 2:
            raise SchemaError(f"question {qid!r}: choice needs >= 2 criteria")
        options = [(str(k), _text(v) if v else "") for k, v in crit.items()]
    elif t == "score":
        if not isinstance(crit, list) or len(crit) < 2:
            raise SchemaError(f"question {qid!r}: score needs a list of >= 2 levels")
        options = [(str(i), _text(v)) for i, v in enumerate(crit)]
    else:
        crit = crit or {}
        if not isinstance(crit, dict):
            raise SchemaError(f"question {qid!r}: noul criteria must be {{'true':..., 'false':...}}")
        options = [("true", _text(crit.get("true", "yes, it holds"))),
                   ("false", _text(crit.get("false", "no, it does not hold")))]
    if len(options) > 20:
        raise SchemaError(f"question {qid!r}: at most 20 options are supported")
    return {"type": t, "instructions": _text(instr), "options": options}


def normalize_request(req):
    if not isinstance(req, dict):
        raise SchemaError("request must be a JSON object")
    text = state_to_text(req.get("state"))
    qs = req.get("questions")
    if not isinstance(qs, dict) or not qs:
        raise SchemaError("'questions' must be a non-empty object")
    return text, {qid: normalize_question(qid, q) for qid, q in qs.items()}


def answer_from_probs(qtype, probs):
    """Build a typed answer from {label: probability} (probabilities need not sum to 1)."""
    total = sum(probs.values())
    if total <= 0:
        raise ValueError("no probability mass on any option")
    p = {k: v / total for k, v in probs.items()}
    if qtype == "choice":
        best = max(p, key=p.get)
        return {"type": "choice", "choice": best, "confidence": round(p[best], 4),
                "probabilities": {k: round(v, 4) for k, v in p.items()}}
    if qtype == "score":
        best = max(p, key=p.get)
        return {"type": "score", "score": round(sum(int(k) * v for k, v in p.items()), 4),
                "level": int(best), "confidence": round(p[best], 4),
                "probabilities": {k: round(v, 4) for k, v in p.items()}}
    pt = p.get("true", 0.0)
    return {"type": "noul", "noul": round(pt, 4), "confidence": round(max(pt, 1 - pt), 4)}


def normalize_answer(qtype, a):
    """Fill in ``confidence`` (and choice/level) for answers coming from remote backends."""
    a = dict(a)
    a.setdefault("type", qtype)
    probs = a.get("probabilities")
    if qtype == "noul" and isinstance(a.get("noul"), (int, float)):
        a.setdefault("confidence", round(max(a["noul"], 1 - a["noul"]), 4))
    elif isinstance(probs, dict) and probs:
        a.setdefault("confidence", round(max(probs.values()), 4))
        if qtype == "choice":
            a.setdefault("choice", max(probs, key=probs.get))
    a.setdefault("confidence", None)
    return a


def label_of(answer):
    """The discrete decision of an answer: choice label, score level, or 'true'/'false'."""
    t = answer.get("type")
    if t == "choice":
        return answer.get("choice")
    if t == "score":
        if "level" in answer:
            return str(answer["level"])
        return str(round(answer.get("score", 0)))
    if t == "noul" and isinstance(answer.get("noul"), (int, float)):
        return "true" if answer["noul"] >= 0.5 else "false"
    return None
