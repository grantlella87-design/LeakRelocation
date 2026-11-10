# Setup

```bat
python bootstrap.py
```

That is the whole of it. `bootstrap.py` asks Zscaler whether it is in the path,
creates `.venv` if there is not one, and installs `requirements.txt` into it —
adding `--proxy http://zscaler.nationalgrid.com:80` to pip when Zscaler is active
and leaving it off when it is not. It uses the standard library only, because it
is the script that runs before anything is installed.

| Flag | Effect |
| --- | --- |
| `--dry-run` | Print the commands, change nothing. |
| `--force-proxy` | Use the proxy without checking. |
| `--no-proxy` | Never use the proxy. |
| `--skip-check` | Do not contact Zscaler at all. |
| `--venv PATH` | Somewhere other than `.venv`. |

The check is `http://ip.zscaler.com/`, Zscaler's own **My IP Address** page, read
for the sentence it returns when you are *not* behind it:

    The request received from you didn't come from a Zscaler IP therefore you are
    not going through the Zscaler proxy service.

Deliberately **http**, not https: when Zscaler is active it re-signs TLS, and this
runs before the venv exists, so there is no `truststore` yet to verify against the
Windows certificate store. An https probe would fail in exactly the case that
needs the proxy. Because plain http can be hijacked, a reply that does not look
like Zscaler's page at all — a captive portal, a block page — is reported as
`unknown` rather than taken for either answer, and `unknown` installs directly and
tells you to re-run with `--force-proxy` if that cannot reach PyPI.

Doing it by hand instead:

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Install from `requirements.txt` rather than picking packages by hand: `truststore`
is in there for a reason. The corporate proxy re-signs HTTPS with an internal root
that Windows trusts and certifi does not, so without it every request to
`gis.nationalgrid.com` ends in

    SSLCertVerificationError: self-signed certificate in certificate chain

`auth.make_session()` warns when it cannot import it — `truststore injection
failed: No module named 'truststore'` is that warning, and the sign-in traceback
that follows is its consequence, not a separate problem.

If pip itself fails on certificates rather than on reaching the host, the proxy
flag will not help: pip does not use `truststore`. Point it at the corporate root
instead, with `PIP_CERT` or `pip config set global.cert <path to the PEM>`.
