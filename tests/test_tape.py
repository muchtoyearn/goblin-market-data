import tape as tp

T = 1_795_000_000  # seconds


def trade(tid, px, sz, side, buyer, seller, t=T):
    return {"coin": "xyz:NVDA", "px": str(px), "sz": str(sz), "side": side, "time": int(t * 1000), "tid": tid,
            "users": [buyer, seller]}


def test_aggregate_floor_dedup_and_roundtrip():
    tape = tp.Tape()
    for i in range(20):  # whale buys 20 x $10k as taker = $200k net
        assert tape.add_trade(trade(i, 100, 100, "B", "0xWHALE", f"0xmm{i % 2}", T + i))
    assert not tape.add_trade(trade(3, 100, 100, "B", "0xWHALE", "0xmm1", T + 3))  # reconnect duplicate
    rows = tape.merged()
    keys = {k[2] for k in rows}
    assert "0xwhale" in keys and "0xmm0" not in keys  # MMs are each -$100k net, under the $150k floor
    r = rows[(int(T // 3600), "xyz:NVDA", "0xwhale")]
    assert r[0] == 200_000 and r[2] == 200_000 and r[3] == 20
    back = tp.decode_day(tp.encode_day(rows))
    rb = back[(int(T // 3600), "xyz:NVDA", "0xwhale")]
    assert rb[0] == 200_000 and rb[3] == 20 and rb[5] == 100.0


def test_candidates_need_size():
    tape = tp.Tape()
    for i in range(120):
        tape.add_trade(trade(i, 100, 100, "B", "0xnew", "0xother", T + i * 60))
    c = tape.candidates(T + 7200)
    assert c and c[0]["address"] == "0xnew" and c[0]["net_usd"] >= 1_000_000 and c[0]["side"] == "long"
