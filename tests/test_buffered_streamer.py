"""Engine end-to-end tests: BufferedByteStreamer against fake sessions."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import (FakeClient, FakeFileId, collect, load_buffered_streamer,
                     load_stream_buffer, run, settle)

sb = load_stream_buffer()
bs, stubs = load_buffered_streamer(sb)

CHUNK = 1024
DATA = bytes((i * 7) % 256 for i in range(10 * CHUNK))


def fresh():
    """Isolate tests: default policy and an empty shared chunk cache."""
    sb.refresh_policy(None)
    sb.get_cache().clear()


def make_streamer(clients):
    """clients: list of FakeClient; first is primary, rest become the pool."""
    primary = clients[0]
    streamer = bs.BufferedByteStreamer(primary, 0)
    extras = []
    for idx, client in enumerate(clients[1:], start=1):
        s = bs.BufferedByteStreamer(client, idx)
        fid = FakeFileId()
        client.known_file_id = fid
        extras.append((idx, s, fid))
    return streamer, extras


async def stream(streamer, extras, data_len_chunks=10, file_id=None, **kw):
    file_id = file_id or FakeFileId()
    gen = await streamer.prefetch_stream(
        file_id, 0, 0, 0, CHUNK, data_len_chunks, CHUNK,
        prefetch=4, parallelism=2, stream_id=kw.pop("stream_id", "test-stream"),
        extra_clients=extras, **kw,
    )
    return await collect(gen)


def entry(stream_id="test-stream"):
    return stubs["custom_dl"].ACTIVE_STREAMS[stream_id]


def test_in_order_delivery_across_clients():
    async def main():
        fresh()
        clients = [FakeClient(DATA), FakeClient(DATA)]
        streamer, extras = make_streamer(clients)
        out = await stream(streamer, extras)
        await settle()
        assert out == DATA, "bytes must be exact and in order"
        assert entry()["status"] == "finished"
        assert entry()["total_bytes"] == len(DATA)
        assert entry()["buffered"] is True
        total_calls = sum(c.session.calls for c in clients)
        assert total_calls == 10, total_calls
    run(main())


def test_part_cuts_single_part():
    async def main():
        fresh()
        clients = [FakeClient(DATA)]
        streamer, extras = make_streamer(clients)
        file_id = FakeFileId()
        gen = await streamer.prefetch_stream(
            file_id, 0, 0, 100, 900, 1, CHUNK, prefetch=1, parallelism=1,
            stream_id="cuts",
        )
        out = await collect(gen)
        await settle()
        assert out == DATA[:CHUNK][100:900]
    run(main())


def test_cache_serves_second_stream_without_telegram():
    async def main():
        fresh()
        clients = [FakeClient(DATA), FakeClient(DATA)]
        streamer, extras = make_streamer(clients)
        out1 = await stream(streamer, extras, stream_id="first")
        calls_after_first = sum(c.session.calls for c in clients)
        out2 = await stream(streamer, extras, stream_id="second")
        calls_after_second = sum(c.session.calls for c in clients)
        await settle()
        assert out1 == out2 == DATA
        assert calls_after_first == 10
        assert calls_after_second == 10, "second stream must be all cache hits"
    run(main())


def test_eof_when_file_shorter_than_part_count():
    async def main():
        fresh()
        short = DATA[:3 * CHUNK]
        clients = [FakeClient(short)]
        streamer, extras = make_streamer(clients)
        out = await stream(streamer, extras, data_len_chunks=5, stream_id="eof")
        await settle()
        assert out == short
        assert entry("eof")["status"] == "finished"
    run(main())


def test_failed_chunk_is_error_never_fake_eof():
    async def main():
        fresh()
        clients = [FakeClient(DATA, {"fail_always": True})]
        streamer, extras = make_streamer(clients)
        out = await stream(streamer, extras, stream_id="fail")
        await settle()
        assert out == b""
        assert entry("fail")["status"] == "error", entry("fail")["status"]
        assert sb.snapshot()["metrics"]["streams_failed"] >= 1
    run(main())


def test_flooded_client_is_shed_and_stream_completes():
    async def main():
        fresh()
        clients = [FakeClient(DATA, {"flood_always": True}), FakeClient(DATA)]
        streamer, extras = make_streamer(clients)
        out = await stream(streamer, extras, stream_id="flood")
        await settle()
        assert out == DATA
        e = entry("flood")
        assert e["status"] == "finished"
        assert 0 in e["buffer_shed"], e["buffer_shed"]
        assert e["buffer_floods"] >= 1
    run(main())


def test_small_flood_wait_is_honoured_then_recovers():
    async def main():
        fresh()
        clients = [FakeClient(DATA, {"flood_once": True})]
        streamer, extras = make_streamer(clients)
        out = await stream(streamer, extras, data_len_chunks=3, stream_id="floodonce")
        await settle()
        assert out == DATA[:3 * CHUNK]
        assert entry("floodonce")["status"] == "finished"
    run(main())


def test_per_client_cap_is_strict():
    async def main():
        # custom policy: window of 8 but only 2 concurrent fetches allowed
        # on the single client — the cap must never be exceeded (I5)
        sb.refresh_policy({"buffer_profile": "custom", "buffer_workers": 8,
                           "per_client_cap": 2, "buffer_readahead_mb": 16,
                           "buffer_cache_mb": 48})
        sb.get_cache().clear()
        clients = [FakeClient(DATA, {"delay": 0.05})]
        streamer, extras = make_streamer(clients)
        out = await stream(streamer, extras, stream_id="cap")
        await settle()
        assert out == DATA
        assert clients[0].session.max_concurrent <= 2, clients[0].session.max_concurrent
        assert entry("cap")["status"] == "finished"
    run(main())


def test_disabled_policy_delegates_to_upstream():
    async def main():
        sb.refresh_policy({"buffer_enabled": False})
        clients = [FakeClient(DATA)]
        streamer, extras = make_streamer(clients)
        gen = await streamer.prefetch_stream(
            FakeFileId(), 0, 0, 0, CHUNK, 10, CHUNK, prefetch=4, parallelism=2,
            stream_id="off", extra_clients=extras,
        )
        out = await collect(gen)
        await settle()
        assert out == b"upstream"  # the fake upstream generator ran
        assert len(streamer.upstream_calls) == 1
        fresh()
    run(main())


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"{len(fns)} streamer tests passed")
