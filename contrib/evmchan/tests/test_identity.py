# SPDX-License-Identifier: MIT
"""The peer identity handshake (SPEC 5.4): every EVM node runs lightningd
with --network=regtest, so evmbackend tells peers its EVM chain, contract and
asset in a custom message and refuses channels with peers on another one.
No EVM node: the encoding, the matching, and the hooks' decisions, with a
fake lightningd RPC."""
import threading
import time

import pytest

import evm
import evmbackend as eb
from conftest import wait_until
from test_backend import FakeLn, GOOD_INFO, Log, offline_backend, open_payload, options, refused

CONTRACT = evm.checksum_address('0x' + '11' * 20)       # offline_backend's
OTHER = evm.checksum_address('0x' + '22' * 20)
TOKEN = evm.checksum_address('0x' + 'ab' * 20)
PEER = '02' * 33
OURS = eb.Identity(31337, CONTRACT, None, eb.SAT_WEI)


def ln():
    return FakeLn({'sendcustommsg': {'status': 'sent'}, 'disconnect': {}})


def backend(tmp_path, fake=None, log=None, wait=0.2):
    b = offline_backend(tmp_path, ln=fake, log=log, identity=True)
    b.identity_wait = wait
    return b


def msg(ident):
    return eb.encode_identity(ident).hex()


def disconnects(fake):
    return [p for m, p in fake.calls if m == 'disconnect']


# ------------------------------------------------------------ encoding

def test_encode_decode_roundtrip():
    tok = eb.Identity(8453, OTHER, TOKEN, 10**4)
    for ident in (OURS, tok, eb.Identity(2**64 - 1, CONTRACT, TOKEN, 2**256 - 1)):
        raw = eb.encode_identity(ident)
        assert len(raw) == 83 and raw[:3] == bytes.fromhex('ffe901')
        assert eb.decode_identity(raw) == ident
    raw = eb.encode_identity(OURS)
    assert raw[3:11] == (31337).to_bytes(8, 'big') and raw[11:31] == bytes.fromhex('11' * 20)
    assert raw[31:51] == bytes(20)                       # ETH: no token
    assert raw[51:] == eb.SAT_WEI.to_bytes(32, 'big')
    # Bytes after version 1's fields are room to extend it: ignored.
    assert eb.decode_identity(raw + b'\x01\x02extension') == OURS


def test_decode_refuses_malformed_and_unknown_versions():
    raw = eb.encode_identity(OURS)
    for bad in (b'', b'\xff', b'\xff\xe9', raw[:-1], raw[:3],
                b'\xff\xeb' + raw[2:],                    # another message type
                raw[:3] + bytes(8) + raw[11:],            # chain id 0
                raw[:11] + bytes(20) + raw[31:],          # contract 0
                raw[:51] + bytes(32)):                    # units per sat 0
        with pytest.raises(ValueError) as e:
            eb.decode_identity(bad)
        assert not isinstance(e.value, eb.UnknownIdentityVersion), bad
    for version in (0, 2, 255):
        with pytest.raises(eb.UnknownIdentityVersion, match='version {}'.format(version)):
            eb.decode_identity(raw[:2] + bytes([version]) + raw[3:])


def test_matching():
    assert OURS.matches(eb.Identity(31337, CONTRACT.lower(), None, eb.SAT_WEI))
    assert OURS.matches(eb.decode_identity(eb.encode_identity(OURS)))
    for other in (OURS._replace(chain_id=1), OURS._replace(contract=OTHER), OURS._replace(token=TOKEN),
                  OURS._replace(units_per_sat=1), None, (31337, CONTRACT, None, eb.SAT_WEI)):
        assert not OURS.matches(other), other
    tok = OURS._replace(token=TOKEN)
    assert tok.matches(tok._replace(token=TOKEN.lower())) and not tok.matches(tok._replace(token=OTHER))
    assert 'ETH' in OURS.describe() and TOKEN in tok.describe() and CONTRACT in tok.describe()


def test_option(tmp_path):
    assert eb.Config.from_options(options(), str(tmp_path), {}, None).require_peer_identity     # default on
    for v, want in ((True, True), ('true', True), (False, False), ('false', False)):
        cfg = eb.Config.from_options(options(**{eb.REQUIRE_PEER_IDENTITY: v}), str(tmp_path), {}, None)
        assert cfg.require_peer_identity is want
    with pytest.raises(eb.ConfigError, match=eb.REQUIRE_PEER_IDENTITY):
        eb.Config.from_options(options(**{eb.REQUIRE_PEER_IDENTITY: 'maybe'}), str(tmp_path), {}, None)


# ------------------------------------------------------------ the handshake

def test_peer_connected_sends_our_identity(tmp_path):
    fake = ln()
    b = backend(tmp_path, fake)
    b.token, b.units_per_sat = TOKEN, 10**4
    assert b.peer_connected({'id': PEER, 'features': GOOD_INFO['our_features']['init']}) == {'result': 'continue'}
    wait_until(lambda: fake.calls)
    want = eb.encode_identity(eb.Identity(31337, CONTRACT, TOKEN, 10**4)).hex()
    assert fake.calls == [('sendcustommsg', {'node_id': PEER, 'msg': want})]
    # A peer sent away for its features gets nothing.
    assert b.peer_connected({'id': '03' * 33, 'features': ''})['result'] == 'disconnect'
    time.sleep(0.1)
    assert len(fake.calls) == 1


def test_send_failure_is_logged(tmp_path):
    def gone(params):
        raise RuntimeError('Peer is not connected')
    log = Log()
    b = backend(tmp_path, FakeLn({'sendcustommsg': gone}), log=log)
    b.send_identity(PEER)
    assert any('cannot send our EVM chain/contract identity' in m for m in log.at('unusual'))


def test_custommsg_records_and_disconnects_a_mismatch(tmp_path):
    fake, log = ln(), Log()
    b = backend(tmp_path, fake, log=log)
    # Not ours: untouched.
    for payload in ('aaff00', '', 'zz', 'ff'):
        assert b.custommsg(PEER, payload) == {'result': 'continue'}
    assert b._peer_ids == {}
    assert b.custommsg(PEER, msg(OURS)) == {'result': 'continue'}
    assert b._peer_ids[PEER]['status'] == 'matched' and not disconnects(fake)
    theirs, peer2 = OURS._replace(contract=OTHER), '03' + 'cd' * 32
    assert b.custommsg(peer2.upper(), msg(theirs)) == {'result': 'continue'}     # ids match in any case
    assert b._peer_ids[peer2]['status'] == 'mismatched'
    wait_until(lambda: disconnects(fake))
    assert disconnects(fake) == [{'id': peer2.upper(), 'force': True}]
    line = [m for m in log.at('unusual') if 'different EVM chain/contract' in m]
    assert len(line) == 1 and OTHER in line[0] and CONTRACT in line[0] and line[0].endswith('disconnecting')


def test_custommsg_unreadable(tmp_path):
    fake, log = ln(), Log()
    b = backend(tmp_path, fake, log=log)
    raw = eb.encode_identity(OURS)
    b.custommsg(PEER, (raw[:2] + b'\x02' + raw[3:]).hex())
    assert b._peer_ids[PEER]['status'] == 'unreadable'
    assert 'version 2' in b.openchannel(open_payload())['error_message']
    b.custommsg(PEER, raw[:20].hex())
    assert 'malformed' in b.openchannel(open_payload())['error_message']
    assert not disconnects(fake)                     # refused, not sent away
    b.custommsg(PEER, raw.hex())
    assert b.openchannel(open_payload()) == {'result': 'continue'}


# ------------------------------------------------------------ the hooks' decisions

def test_inbound_opens(tmp_path):
    b = backend(tmp_path, ln())
    for hook in (b.openchannel, b.openchannel2):
        b._peer_ids.clear()
        t = time.monotonic()
        r = hook(open_payload())
        assert time.monotonic() - t >= b.identity_wait
        assert r['result'] == 'reject' and 'has not identified its EVM chain/contract' in r['error_message']
        b.custommsg(PEER, msg(OURS))
        assert hook(open_payload()) == {'result': 'continue'}
        b.custommsg(PEER, msg(OURS._replace(chain_id=84532)))
        r = hook(open_payload())
        assert r['result'] == 'reject' and 'EVM chain 84532' in r['error_message']
        assert 'EVM chain 31337' in r['error_message']
    # Other refusals still come first.
    assert 'channel_type' in b.openchannel(open_payload(channel_type={'bits': [12]}))['error_message']


def test_open_waits_for_a_message_in_flight(tmp_path):
    b = backend(tmp_path, ln(), wait=5)
    late = threading.Timer(0.2, b.custommsg, (PEER, msg(OURS)))
    late.start()
    t = time.monotonic()
    assert b.openchannel(open_payload()) == {'result': 'continue'}
    assert time.monotonic() - t < 4


def test_reconnect_must_identify_again(tmp_path):
    b = backend(tmp_path, ln())
    b.custommsg(PEER, msg(OURS))
    b.peer_disconnected(PEER)
    r = b.openchannel(open_payload())                # the old connection's message doesn't count
    assert r['result'] == 'reject' and 'has not identified' in r['error_message']
    b.custommsg(PEER, msg(OURS))
    assert b.openchannel(open_payload()) == {'result': 'continue'}
    # A mismatch is remembered after the disconnect it caused ...
    b.custommsg(PEER, msg(OURS._replace(token=TOKEN)))
    b.peer_disconnected(PEER)
    assert 'different EVM chain/contract' in b.openchannel(open_payload())['error_message']
    # ... until the peer comes back on ours.
    b.custommsg(PEER, msg(OURS))
    assert b.openchannel(open_payload()) == {'result': 'continue'}
    b.peer_disconnected(None)                        # ignored


def fc(method, peer=PEER):
    if method == 'multifundchannel':
        return {'method': method, 'params': {'destinations': [{'id': 'x', 'amount': 1}, {'id': peer, 'amount': 1}]}}
    return {'method': method, 'params': {'id': peer, 'amount': 1000}}


def test_outbound_opens(tmp_path):
    b = backend(tmp_path, ln())
    rc = b.rpc_command
    # Unknown peer: fundchannel and multifundchannel may connect first, so
    # they go on; fundchannel_start and openchannel_init (after the connect)
    # wait for the message, then refuse.
    for m in ('fundchannel', 'multifundchannel'):
        assert rc(fc(m)) == {'result': 'continue'}, m
    for m in ('fundchannel_start', 'openchannel_init'):
        r = rc(fc(m))
        assert refused(r) and 'has not identified' in r['return']['error']['message'], m
    b.custommsg(PEER, msg(OURS))
    for m in ('fundchannel', 'fundchannel_start', 'openchannel_init'):
        assert rc(fc(m)) == {'result': 'continue'}, m
        assert rc(fc(m, PEER + '@127.0.0.1:9735')) == {'result': 'continue'}, m
    b.custommsg('03' * 33, msg(OURS))
    assert rc(fc('multifundchannel', '03' * 33)) == {'result': 'continue'}
    # Mismatched: refused early by every one, connected or not.
    b.custommsg(PEER, msg(OURS._replace(units_per_sat=1)))
    b.peer_disconnected(PEER)
    for m in ('fundchannel', 'multifundchannel', 'fundchannel_start', 'openchannel_init'):
        r = rc(fc(m))
        assert refused(r) and 'different EVM chain/contract' in r['return']['error']['message'], m
    # Not opens: untouched.
    assert rc({'method': 'withdraw', 'params': ['x', 'all']}) == {'result': 'continue'}


def test_enforcement_off(tmp_path):
    fake, log = ln(), Log()
    b = backend(tmp_path, fake, log=log)
    b.cfg.require_peer_identity = False
    assert b.openchannel(open_payload()) == {'result': 'continue'}           # no wait either
    assert b.rpc_command(fc('fundchannel_start')) == {'result': 'continue'}
    b.custommsg(PEER, msg(OURS._replace(contract=OTHER)))
    assert any('evm-require-peer-identity is off' in m for m in log.at('unusual'))
    assert b.openchannel2(open_payload()) == {'result': 'continue'}
    assert b.rpc_command(fc('fundchannel')) == {'result': 'continue'}
    time.sleep(0.1)
    assert not disconnects(fake)
    # Ours is still sent: peers that enforce it need it.
    b.peer_connected({'id': PEER, 'features': GOOD_INFO['our_features']['init']})
    wait_until(lambda: any(m == 'sendcustommsg' for m, _ in fake.calls))
