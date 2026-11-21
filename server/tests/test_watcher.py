"""watcher 단위 테스트 — 클러스터 없이 가짜 list/watch로 검증."""

import json
import threading
import time
from types import SimpleNamespace

from kubernetes.client.exceptions import ApiException

from app.k8s import summarize
from app.k8s.cache import ResourceCache
from app.k8s.registry import KindSpec
from app.k8s.watcher import ResourceWatcher


def pod(name, rv, phase="Running"):
    return {
        "kind": "Pod",
        "metadata": {"name": name, "namespace": "mentor", "resourceVersion": rv},
        "status": {"phase": phase},
    }


class FakeList:
    """호출될 때마다 준비된 list 응답을 순서대로 반환."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, namespace, **kwargs):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        items, rv = self.responses[idx]
        body = {"metadata": {"resourceVersion": rv}, "items": items}
        return SimpleNamespace(data=json.dumps(body).encode())


class FakeWatch:
    """stream() 호출마다 준비된 스크립트를 하나씩 소비. 스크립트가 끝나면 stop_event를 세움."""

    def __init__(self, scripts, stop_event, calls):
        self.scripts = scripts
        self.stop_event = stop_event
        self.calls = calls

    def stream(self, func, **kwargs):
        self.calls.append(kwargs)
        if not self.scripts:
            self.stop_event.set()
            return
        script = self.scripts.pop(0)
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item

    def stop(self):
        pass


def run_watcher(list_responses, scripts):
    stop = threading.Event()
    cache = ResourceCache()
    fake_list = FakeList(list_responses)
    calls = []
    spec = KindSpec("pods", "Pod", "v1", fake_list, summarize.pod_status)
    w = ResourceWatcher(spec, "mentor", cache, stop, timeout_seconds=5,
                        watch_factory=lambda: FakeWatch(scripts, stop, calls))
    w._fail = lambda msg, backoff: (setattr(w, "last_error", msg), backoff)[1]  # 테스트에선 대기 없음
    w.start()
    w.join(5)
    assert not w.is_alive()
    return w, cache, fake_list, calls


def ev(etype, obj):
    return {"type": etype, "raw_object": obj}


def test_list_then_watch_events_update_cache():
    w, cache, fake_list, calls = run_watcher(
        [([pod("a", "1"), pod("b", "1")], "10")],
        [[
            ev("ADDED", pod("c", "11")),
            ev("MODIFIED", pod("a", "12", phase="Failed")),
            ev("DELETED", pod("b", "13")),
            ev("BOOKMARK", {"metadata": {"resourceVersion": "20"}}),
        ]],
    )
    assert w.synced.is_set()
    assert sorted(o["metadata"]["name"] for o in cache.list()) == ["a", "c"]
    assert cache.get("a")["status"]["phase"] == "Failed"
    assert w.resource_version == "20"
    # watch는 list에서 받은 rv로 시작, bookmark 허용
    assert calls[0]["resource_version"] == "10"
    assert calls[0]["allow_watch_bookmarks"] is True
    assert fake_list.calls == 1


def test_list_items_get_kind_and_api_version():
    _, cache, _, _ = run_watcher([([{"metadata": {"name": "a"}}], "1")], [])
    assert cache.get("a")["kind"] == "Pod"
    assert cache.get("a")["apiVersion"] == "v1"


def test_410_exception_triggers_relist_and_replaces_cache():
    # 첫 list: a,b / 410 후 재list: b,c  → 놓친 a 삭제가 반영되어야 함
    w, cache, fake_list, calls = run_watcher(
        [([pod("a", "1"), pod("b", "1")], "10"), ([pod("b", "30"), pod("c", "30")], "30")],
        [[ApiException(status=410, reason="Expired")], []],
    )
    assert fake_list.calls == 2
    assert w.relist_count == 1
    assert sorted(o["metadata"]["name"] for o in cache.list()) == ["b", "c"]
    assert calls[1]["resource_version"] == "30"


def test_410_error_event_triggers_relist():
    w, cache, fake_list, calls = run_watcher(
        [([pod("a", "1")], "10"), ([], "40")],
        [[ev("ERROR", {"code": 410, "reason": "Expired", "message": "too old resource version"})], []],
    )
    assert fake_list.calls == 2
    assert w.relist_count == 1
    assert cache.list() == []
    assert calls[1]["resource_version"] == "40"


def test_other_error_retries_watch_without_relist():
    w, cache, fake_list, calls = run_watcher(
        [([pod("a", "1")], "10")],
        [[ConnectionResetError("boom")], [ev("ADDED", pod("b", "11"))]],
    )
    assert fake_list.calls == 1          # rv가 유효하므로 list 다시 안 함
    assert calls[1]["resource_version"] == "10"
    assert cache.get("b") is not None
    assert "ConnectionResetError" in w.last_error


def test_stream_timeout_rewatches_from_last_rv():
    w, _, fake_list, calls = run_watcher(
        [([], "10")],
        [[ev("ADDED", pod("a", "15"))], [ev("ADDED", pod("b", "16"))]],
    )
    assert fake_list.calls == 1
    assert [c["resource_version"] for c in calls[:2]] == ["10", "15"]


def test_list_failure_retries_list():
    stop = threading.Event()
    cache = ResourceCache()
    state = {"n": 0}

    def flaky_list(namespace, **kw):
        state["n"] += 1
        if state["n"] == 1:
            raise ApiException(status=500, reason="boom")
        return SimpleNamespace(data=json.dumps({"metadata": {"resourceVersion": "5"}, "items": []}).encode())

    spec = KindSpec("pods", "Pod", "v1", flaky_list, summarize.pod_status)
    w = ResourceWatcher(spec, "mentor", cache, stop, watch_factory=lambda: FakeWatch([], stop, []))
    w._fail = lambda msg, backoff: backoff
    w.start()
    w.join(5)
    assert state["n"] == 2
    assert w.synced.is_set()
