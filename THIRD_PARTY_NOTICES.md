# Third-party notices

The root MIT License covers Studio Seventeen project code. It does not replace
third-party licenses. Keep dependency copyright notices and license files when
you redistribute them. This inventory was checked on 2026-09-06.

## Vendored browser client

`static/vendor/socket.io.min.js` is Socket.IO client 4.8.1. Copyright
2014-present Guillermo Rauch and Socket.IO contributors. Its full MIT License
is in `static/vendor/socket.io.LICENSE` and must travel with the client.

The local file matches the npm 4.8.1 distribution after removal of its final
source-map comment. SHA-256:
`13d87b39ee3d93d65eca2ef04729a0538735be0959e1d0e994a135247ea32256`.
Upstream package: https://www.npmjs.com/package/socket.io-client/v/4.8.1.

## Python runtime

Versions below are from `requirements.lock`. Licenses were checked against
installed distribution metadata and license files. Full upstream license files
remain in the installed distributions. The Docker image copies that environment,
including dependency source and distribution license files.

| Package | Version | License |
|---|---|---|
| bidict | 0.23.1 | MPL-2.0 |
| blinker | 1.9.0 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| Flask | 3.1.3 | BSD-3-Clause |
| Flask-SocketIO | 5.6.1 | MIT |
| gunicorn | 23.0.0 | MIT |
| h11 | 0.16.0 | MIT |
| itsdangerous | 2.2.0 | BSD-3-Clause |
| Jinja2 | 3.1.6 | BSD-3-Clause |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| packaging | 26.2 | Apache-2.0 OR BSD-2-Clause |
| pymodbus | 3.7.4 | BSD-3-Clause |
| python-engineio | 4.13.3 | MIT |
| python-socketio | 5.16.3 | MIT |
| simple-websocket | 1.1.0 | MIT |
| Werkzeug | 3.1.8 | BSD-3-Clause |
| wsproto | 1.3.2 | MIT |

bidict remains under MPL-2.0. Its Python source is installed with the package.
Preserve its source and notices. Changes to its covered files require separate
MPL review; this repository does not modify bidict.

The development lock also includes iniconfig, pluggy, pytest, setuptools, and
wheel under MIT, plus Pygments under BSD-2-Clause. Python and the Alpine-based
Docker base image have their own licenses. The final image SBOM must identify
its OS packages; the repository dependency list is not an image SBOM.

The image uses official Python 3.11 on Alpine 3.24, with libuuid pinned to
2.42.3-r1. Alpine uses musl. OS package terms include MIT, BSD, Apache, GPL,
and LGPL licenses; MIT project terms do not replace them. Preserve package
notices and comply with applicable source-distribution obligations. The image
retains the Alpine package inventory at `/lib/apk/db/installed`.

The optional marketing toolchain and its media are not included in this repository.
