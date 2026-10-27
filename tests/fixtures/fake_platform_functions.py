"""Offline stub for cnhkmcp platform_functions, used by driver tests.

Implements just enough of the surface the driver touches: brain_client
with a cookie jar + is_authenticated(), authenticate(), and the
whitelisted command functions. No network, no real credentials.
"""


class _Cookie:
    def __init__(self, name, value, domain, path="/"):
        self.name, self.value, self.domain, self.path = name, value, domain, path


class _Jar:
    def __init__(self):
        self._cookies = []

    def set(self, name, value, domain="", path="/"):
        self._cookies = [c for c in self._cookies if c.name != name]
        self._cookies.append(_Cookie(name, value, domain, path))

    def clear(self):
        self._cookies = []

    def __iter__(self):
        return iter(list(self._cookies))


class _Session:
    def __init__(self):
        self.cookies = _Jar()

    def get(self, url, **kwargs):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                if url.endswith("/data-sets/analyst14"):
                    return {"id": "analyst14", "name": "Estimates",
                            "region": "GLB", "delay": 1, "universe": "TOP3000"}
                return {"count": 3, "results": [{"id": "field_c"}], "next": None}
        return Response()

    def options(self, url, **kwargs):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"actions": {"POST": {"settings": {"children": {
                    "decay": {"type": "integer", "min_value": 0, "max_value": 50},
                    "truncation": {"type": "decimal", "min_value": 0,
                                   "max_value": 1},
                    "pasteurization": {"type": "choice", "choices": [
                        {"value": "ON"}, {"value": "OFF"}]},
                }}}}}
        return Response()


class _Client:
    def __init__(self):
        self.session = _Session()
        self.base_url = "https://api.worldquantbrain.com"

    async def is_authenticated(self):
        return any(c.name == "t" for c in self.session.cookies)


brain_client = _Client()
AUTH_CALLS = []


def authenticate(email, password):
    AUTH_CALLS.append(email)
    if password == "wrong-password":
        raise RuntimeError("401 unauthorized")
    brain_client.session.cookies.set(
        "t", "fake-jwt", domain=".worldquantbrain.com", path="/")
    return {"ok": True}


def get_datasets(**kw):
    return [{"id": "risk60"}, {"id": "pv47"}]


def get_platform_setting_options(**kw):
    return {"instrument_options": [{"InstrumentType": "EQUITY", "Region": "GLB",
             "Delay": 1, "Universe": ["TOP3000"], "Neutralization": ["MARKET"]}]}


def get_datafields(**kw):
    return {"count": 3, "results": [{"id": "field_a"}, {"id": "field_b"}],
            "next": None}


def get_operators(**kw):
    return []


def create_simulation(**kw):
    return {"id": "sim-single"}


def create_multi_simulation(**kw):
    print("platform progress should not enter JSON protocol")
    return {"id": "sim-multi"}


def get_alpha_details(**kw):
    return {"id": "alpha1", "sharpe": 1.7}


def get_alpha_pnl(**kw):
    return {}


def get_alpha_yearly_stats(**kw):
    return []


def check_correlation(**kw):
    return {"max": 0.1}


def get_submission_check(**kw):
    return {"fail_count": 0}


def get_user_alphas(**kw):
    return []


def set_alpha_properties(**kw):
    return {"ok": True}
