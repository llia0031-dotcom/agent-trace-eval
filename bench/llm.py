"""Calling the model: one client, per-call records for the trace, and a pool that sends requests as
Message Batches (half price).

The API key is read from a file (EVAL_ENV_FILE, default eval/.env), never from the shell environment,
so a run cannot pick up credentials that belong to other tools. The key is never printed or stored.
The batch pool is the one used in creator-skills and grounded-business-review (same author, MIT).

Every agent the bench runs is routed through create() here (see the adapters), so all three task
families share one model setting, one price table and one record of calls.
"""
from __future__ import annotations

import json
import os
import threading
import time
from functools import lru_cache
from pathlib import Path

from . import config

MAX_TOKENS = 16000
POOL = None                  # a BatchPool while a batch run is going
MODEL = config.MODEL         # set by the runner for a configuration that uses another model
_local = threading.local()   # the call records of the trial running in this thread


def _read_key() -> str:
    env_file = Path(os.environ.get("EVAL_ENV_FILE") or (config.EVAL / ".env"))
    if not env_file.is_absolute():
        env_file = (config.EVAL / env_file).resolve()
    if not env_file.exists():
        raise SystemExit(f"API key file not found: {env_file}. Put a line ANTHROPIC_API_KEY=... in eval/.env or set EVAL_ENV_FILE.")
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("ANTHROPIC_API_KEY missing in the key file")


@lru_cache(maxsize=1)
def client():
    for k in [k for k in os.environ if k.startswith("ANTHROPIC_")]:
        del os.environ[k]
    import anthropic

    return anthropic.Anthropic(api_key=_read_key(), base_url="https://api.anthropic.com", max_retries=4)


def cost_of(call: dict, batch: bool | None = None) -> float:
    """What one call cost in USD at the price actually paid (batch requests are half price)."""
    p = config.PRICES[call["model"]]
    full = ((call["input_tokens"] + config.CACHE_WRITE * call["cache_write_tokens"] + config.CACHE_READ * call["cache_read_tokens"])
            * p["input"] + call["output_tokens"] * p["output"])
    return full * config.BATCH_DISCOUNT if (call["batch"] if batch is None else batch) else full


def start_trial(name: str = "") -> list:
    """Start recording the model calls made from this thread; returns the list they are appended to.

    name identifies the trial (run, task, configuration, trial number). Each request is keyed by it and
    its call number; with a journal, an interrupted batch run picks an answer up from the batch already
    submitted instead of sending the request again.
    """
    _local.calls = []
    _local.name = name
    return _local.calls


def create(**params):
    """One Messages request. params are complete request parameters except the model."""
    params = dict(model=MODEL, **params)
    params.setdefault("max_tokens", MAX_TOKENS)
    name = getattr(_local, "name", "")
    key = f"{name}#{len(getattr(_local, 'calls', [])) + 1}" if name else None
    t0 = time.perf_counter()
    if POOL is not None:
        resp = POOL.request(params, key)
        batch = True
    else:
        resp = client().messages.create(**params)
        batch = False
    u = resp.usage
    call = {"model": params["model"], "batch": batch, "seconds": round(time.perf_counter() - t0, 3),
            "input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "stop_reason": resp.stop_reason}
    call["cost_usd"] = round(cost_of(call), 8)
    calls = getattr(_local, "calls", None)
    if calls is not None:
        calls.append(call)
    return resp


class BatchPool:
    """Sends the requests of many concurrent workers as Message Batches.

    Each worker (one trial) runs in its own thread and calls create() as usual. When every running
    worker is waiting, the waiting requests go out as one batch; when it has ended each worker gets its
    message back and carries on (runs its tools, asks again). So each round of the tool loops, across
    all workers, is one batch, and prompts, tools and parameters are those of a normal run. Results are
    matched by custom_id. An errored or expired request is sent again in the next batch, three tries in
    all; an invalid request is not retried.
    """

    def __init__(self, poll_seconds: float = 15.0, log=print, journal: Path | None = None):
        self.cv = threading.Condition()
        self.waiting: dict[str, dict] = {}
        self.running = 0
        self.n = 0
        self.closing = False
        self.poll = poll_seconds
        self.log = log
        self.batches: list[dict] = []
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.journal_path = journal
        self.journal: dict[str, tuple[str, str]] = {}
        self.fetched: dict[str, dict] = {}
        self.reused = 0
        self.io = threading.Lock()
        if journal and journal.exists():
            for line in journal.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = json.loads(line)
                    self.journal[e["key"]] = (e["batch"], e["custom_id"])

    def _from_journal(self, key: str):
        batch_id, custom_id = self.journal[key]
        c = client()
        with self.io:
            if batch_id not in self.fetched:
                while c.messages.batches.retrieve(batch_id).processing_status != "ended":
                    time.sleep(self.poll)
                self.fetched[batch_id] = {r.custom_id: r.result for r in c.messages.batches.results(batch_id)}
        res = self.fetched[batch_id].get(custom_id)
        return res.message if res is not None and res.type == "succeeded" else None

    def start(self):
        self.thread.start()

    def enter(self):
        with self.cv:
            self.running += 1

    def leave(self):
        with self.cv:
            self.running -= 1
            self.cv.notify_all()

    def close(self):
        with self.cv:
            self.closing = True
            self.cv.notify_all()
        self.thread.join()

    def request(self, params: dict, key: str | None = None):
        if key and key in self.journal:
            message = self._from_journal(key)
            if message is not None:
                with self.cv:
                    self.reused += 1
                return message
        with self.cv:
            self.n += 1
            slot = {"params": params, "tries": 0, "done": False, "message": None, "error": None, "key": key}
            self.waiting[f"req-{self.n}"] = slot
            self.cv.notify_all()
            while not slot["done"]:
                self.cv.wait()
        if slot["error"]:
            raise RuntimeError(slot["error"])
        return slot["message"]

    def _loop(self):
        while True:
            with self.cv:
                while not (self.waiting and len(self.waiting) >= self.running) and not (self.closing and not self.waiting):
                    self.cv.wait(timeout=5)
                if self.closing and not self.waiting:
                    return
                send, self.waiting = self.waiting, {}
            try:
                results = self._send(send)
            except Exception as e:  # the batch itself failed: its requests are tried again, then stored as failures
                results = {}
                self.log(f"batch failed: {type(e).__name__}: {e}")
            with self.cv:
                for cid, slot in send.items():
                    res = results.get(cid)
                    if res is not None and res.type == "succeeded":
                        slot["message"], slot["done"] = res.message, True
                        continue
                    slot["tries"] += 1
                    why = "missing" if res is None else res.type
                    if res is not None and res.type == "errored":
                        err = getattr(res.error, "error", None)
                        why += f" ({getattr(err, 'type', '')}: {getattr(err, 'message', '')})"
                    if slot["tries"] < 3 and "invalid_request" not in why:
                        self.waiting[cid] = slot
                    else:
                        slot["error"], slot["done"] = f"batch request {why}", True
                self.cv.notify_all()

    def _send(self, send: dict) -> dict:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        c = client()
        batch = c.messages.batches.create(requests=[
            Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**slot["params"])) for cid, slot in send.items()])
        if self.journal_path:
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as f:
                for cid, slot in send.items():
                    if slot.get("key"):
                        f.write(json.dumps({"key": slot["key"], "batch": batch.id, "custom_id": cid}) + "\n")
                        self.journal[slot["key"]] = (batch.id, cid)
        t0 = time.time()
        while True:
            b = c.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            time.sleep(self.poll)
        results = {r.custom_id: r.result for r in c.messages.batches.results(batch.id)}
        counts = b.request_counts
        self.batches.append({"requests": len(send), "minutes": round((time.time() - t0) / 60, 1),
                             "succeeded": counts.succeeded, "errored": counts.errored, "expired": counts.expired})
        self.log(f"batch {len(self.batches)}: {len(send)} requests, ended after {(time.time() - t0) / 60:.1f} min "
                 f"({counts.succeeded} succeeded, {counts.errored} errored, {counts.expired} expired)")
        return results
