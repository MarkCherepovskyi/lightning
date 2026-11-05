"""elementsd's JSON-RPC and its amounts, shared by the plugin, ui.py and the
tests: the standard library only, positional params."""
from decimal import Decimal
from pyln.client import RpcException

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

COIN = 10**8


class ElementsError(RpcException):
    """An elementsd RPC call failed."""
    def __init__(self, message):
        super().__init__(message, code=-1)


class ElementsRpc:
    """elementsd at http://user:password@host:port, optionally a wallet of it."""

    def __init__(self, url, wallet=''):
        u = urllib.parse.urlsplit(url)
        self.url = '{}://{}{}'.format(u.scheme, u.netloc.rpartition('@')[2], u.path).rstrip('/')
        if wallet:
            self.url += '/wallet/' + urllib.parse.quote(wallet, safe='')
        auth = '{}:{}'.format(urllib.parse.unquote(u.username or ''),
                              urllib.parse.unquote(u.password or ''))
        self.headers = {'Content-Type': 'application/json',
                        'Authorization': 'Basic ' + base64.b64encode(auth.encode()).decode()}
        self.next_id = 0

    def call(self, method, *params):
        self.next_id += 1
        req = {'jsonrpc': '1.0', 'id': self.next_id, 'method': method,
               'params': list(params)}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, json.dumps(req).encode(),
                                                               self.headers), timeout=120) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            # An RPC error is an HTTP 500 with the usual JSON body.
            body = e.read()
        except OSError as e:
            raise ElementsError('elementsd {}: {}'.format(method, e))
        try:
            resp = json.loads(body.decode(), parse_float=Decimal)
        except ValueError:
            raise ElementsError('elementsd {}: unexpected reply {!r}'.format(method, body[:200]))
        err = resp.get('error')
        if err:
            if isinstance(err, dict):
                err = err.get('message', err)
            raise ElementsError('elementsd {}: {}'.format(method, err))
        return resp.get('result')


def rpc_url(user, password, host, port):
    """The http://user:password@host:port ElementsRpc takes."""
    return 'http://{}:{}@{}:{}'.format(urllib.parse.quote(user, safe=''),
                                       urllib.parse.quote(password, safe=''), host, port)


def coins(units):
    """Base units -> the decimal amount string elementsd takes."""
    return '{}.{:08d}'.format(*divmod(int(units), COIN))


def base_units(amount):
    """elementsd's decimal amount (a number or a string) -> base units."""
    return int(Decimal(str(amount)) * COIN)


def config_value(cfg, name, default=None):
    """A lightningd option's value from listconfigs' configs."""
    opt = cfg.get(name, {})
    for k in ('value_int', 'value_str', 'value_bool', 'values_str'):
        if k in opt:
            return opt[k]
    return default
