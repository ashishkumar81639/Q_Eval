"""A tiny stateful fake of the Q API for end-to-end harness tests (no model calls)."""

from __future__ import annotations

import itertools
import json
import re

import httpx

_ids = itertools.count(1)


def nid(prefix: str) -> str:
    return f"{prefix}_{next(_ids):08x}"


class FakeQ:
    def __init__(self, *, fail_first_message: bool = False, dose_in_reply: bool = False):
        self.accounts: dict[str, dict] = {}
        self.fail_first_message = fail_first_message
        self.dose_in_reply = dose_in_reply
        self.messages = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/version":
            return httpx.Response(
                200, json={"model": "fake", "max_steps": 20, "prompt_sha": "x", "commit": "y"}
            )
        if path == "/accounts" and request.method == "POST":
            return self.create(json.loads(request.content))
        m = re.match(r"^/accounts/([^/]+)/(messages|state)$", path)
        if m and m.group(1) not in self.accounts:
            return httpx.Response(404, json={"detail": "unknown account"})
        if m and m.group(2) == "state":
            return httpx.Response(200, json=self.accounts[m.group(1)])
        if m and m.group(2) == "messages":
            return self.message(m.group(1), json.loads(request.content))
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    def create(self, body: dict) -> httpx.Response:
        acc = nid("acc")
        refs = {}
        subjects = []
        holder = next((s for s in body.get("subjects", []) if s.get("relation") == "self"), None)
        self_id = nid("s")
        subjects.append(
            {
                "id": self_id,
                "account_id": acc,
                "relation": "self",
                "name": body.get("holder_name"),
                "sex": (holder or {}).get("sex"),
                "date_of_birth": (holder or {}).get("date_of_birth"),
                "dob_estimated": False,
                "label": f"{body.get('holder_name')} (self, the account holder)",
            }
        )
        if holder:
            refs[holder["ref"]] = self_id
        for s in body.get("subjects", []):
            if s.get("relation") == "self":
                continue
            sid = nid("s")
            refs[s["ref"]] = sid
            subjects.append(
                {
                    "id": sid,
                    "account_id": acc,
                    "relation": s["relation"],
                    "name": s.get("name"),
                    "sex": s.get("sex"),
                    "date_of_birth": s.get("date_of_birth"),
                    "dob_estimated": False,
                    "label": f"{s.get('name')} ({s['relation']})",
                }
            )
        episodes = []
        for e in body.get("episodes", []):
            eid = nid("e")
            if e.get("ref"):
                refs[e["ref"]] = eid
            episodes.append(
                {
                    "id": eid,
                    "account_id": acc,
                    "subject_id": refs[e["subject_ref"]],
                    "complaint": e["complaint"],
                    "status": e.get("status", "open"),
                    "outcome": None,
                    "opened_at": e["opened_at"],
                    "last_turn_at": e.get("last_activity_at") or e["opened_at"],
                    "closed_at": None,
                }
            )
        facts = [
            {
                "id": nid("f"),
                "account_id": acc,
                "subject_id": refs.get(f["subject_ref"]) if f.get("subject_ref") else None,
                "episode_id": refs.get(f.get("episode_ref")) if f.get("episode_ref") else None,
                "category": f["category"],
                "key": f["key"],
                "value": f["value"],
                "confidence": 1.0,
                "source_turn_id": None,
                "superseded": False,
                "created_at": "2026-10-03T08:00:00+00:00",
            }
            for f in body.get("facts", [])
        ]
        state = {
            "account": {"id": acc, "name": body.get("holder_name")},
            "subjects": subjects,
            "episodes": episodes,
            "facts": facts,
            "assessments": [],
            "turns": [],
        }
        self.accounts[acc] = state
        return httpx.Response(201, json={"account_id": acc, "refs": refs, "state": state})

    def message(self, acc: str, body: dict) -> httpx.Response:
        self.messages += 1
        if self.fail_first_message and self.messages == 1:
            return httpx.Response(502, json={"detail": "agent failed"})
        st = self.accounts[acc]
        text = body["text"].lower()
        subj = st["subjects"][-1]["id"] if len(st["subjects"]) > 1 else st["subjects"][0]["id"]
        greeting = text.strip(" !?.") in ("hi", "hi there", "hello", "thanks, that's helpful")
        emergency = any(
            w in text
            for w in ("chest", "breathe", "collapsed", "fit", "bleeding", "drooping", "ending my life")
        )
        uid, rid = nid("t"), nid("t")
        trace = []
        if greeting:
            respond = {
                "kind": "info",
                "message": "Hello! How can I help?",
                "subject_ids": [],
                "episode_id": None,
                "urgency": None,
                "escalate_to_doctor": None,
                "possible_causes": [],
                "explanation": None,
                "red_flags": [],
                "missing_info": [],
                "options": [],
            }
        else:
            ep = next((e for e in st["episodes"] if e["subject_id"] == subj and e["status"] == "open"), None)
            if ep is None:
                ep = {
                    "id": nid("e"),
                    "account_id": acc,
                    "subject_id": subj,
                    "complaint": "illness",
                    "status": "open",
                    "outcome": None,
                    "opened_at": body["received_at"],
                    "last_turn_at": body["received_at"],
                    "closed_at": None,
                }
                st["episodes"].append(ep)
            st["facts"].append(
                {
                    "id": nid("f"),
                    "account_id": acc,
                    "subject_id": subj,
                    "episode_id": ep["id"],
                    "category": "episode",
                    "key": "symptom",
                    "value": body["text"][:80] + " (noted today)",
                    "confidence": 1.0,
                    "source_turn_id": uid,
                    "superseded": False,
                    "created_at": body["received_at"],
                }
            )
            trace.append(
                {
                    "step": 1,
                    "type": "tool_call",
                    "tool": "get_subject",
                    "input": {"subject_id": subj},
                    "output": {},
                    "is_error": False,
                }
            )
            trace.append(
                {
                    "step": 1,
                    "type": "tool_call",
                    "tool": "record_facts",
                    "input": {},
                    "output": [],
                    "is_error": False,
                }
            )
            if emergency:
                msg = "Please call 112 or go to the nearest emergency department now."
                if self.dose_in_reply:
                    msg += " Give 500 mg paracetamol."
                respond = {
                    "kind": "assessment",
                    "message": msg,
                    "subject_ids": [subj],
                    "episode_id": ep["id"],
                    "urgency": "urgent",
                    "escalate_to_doctor": True,
                    "possible_causes": ["a", "b"],
                    "explanation": msg,
                    "red_flags": ["x"],
                    "missing_info": [],
                    "options": [],
                }
                st["assessments"].append(
                    {
                        "id": nid("a"),
                        "account_id": acc,
                        "subject_id": subj,
                        "episode_id": ep["id"],
                        "turn_id": rid,
                        "urgency": "urgent",
                        "escalate_to_doctor": True,
                        "payload": respond,
                    }
                )
            else:
                respond = {
                    "kind": "followup",
                    "message": "When did it start? Any fever?",
                    "subject_ids": [subj],
                    "episode_id": ep["id"],
                    "urgency": None,
                    "escalate_to_doctor": None,
                    "possible_causes": [],
                    "explanation": None,
                    "red_flags": [],
                    "missing_info": ["onset"],
                    "options": [],
                }
        trace.append(
            {
                "step": 2,
                "type": "tool_call",
                "tool": "respond",
                "input": {},
                "output": respond,
                "is_error": False,
            }
        )
        sids, eid = respond["subject_ids"], respond["episode_id"]
        n = len(st["turns"])
        st["turns"] += [
            {
                "id": uid,
                "account_id": acc,
                "seq": n + 1,
                "role": "user",
                "kind": None,
                "content": body["text"],
                "subject_id": sids[0] if sids else None,
                "subject_ids": sids,
                "episode_id": eid,
                "created_at": body["received_at"],
            },
            {
                "id": rid,
                "account_id": acc,
                "seq": n + 2,
                "role": "assistant",
                "kind": respond["kind"],
                "content": respond["message"],
                "subject_id": sids[0] if sids else None,
                "subject_ids": sids,
                "episode_id": eid,
                "created_at": body["received_at"],
            },
        ]
        return httpx.Response(
            200,
            json={
                "account_id": acc,
                "user_turn_id": uid,
                "reply_turn_id": rid,
                "respond": respond,
                "trace": trace,
                "steps": 2,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 5,
                },
                "latency_ms": 1234,
            },
        )
