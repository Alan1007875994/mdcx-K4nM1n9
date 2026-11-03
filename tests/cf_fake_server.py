"""Local-only HTTP fixture for bypass/circuit acceptance, using the real curl transport."""

import asyncio
import contextlib
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from curl_cffi import CurlOpt

from mdcx.config.enums import Website
from mdcx.crawlers.base import BaseCrawler
from mdcx.models.types import CrawlerResult
from mdcx.web_async import AsyncWebClient

CHALLENGE = b"<html>just a moment cf-chl</html>"
HOSTS = [f"cold{i}.test" for i in range(6)] + ["slow.test", "plain.test", "missing.test", "mirror.test"]


@dataclass
class Scenario:
    direct_status: int = 503
    direct_body: bytes = CHALLENGE
    direct_delay: float = 0
    mirror_status: int = 503
    mirror_delay: float = 0
    html_status: int = 200
    html_delay: float = 0.05


class FakeBypassServer:
    def __init__(self, log=print):
        self.log = log
        self.scenarios = {host: Scenario() for host in HOSTS}
        self.warmed: set[str] = set()
        self.events: list[dict] = []
        self.tasks: set[asyncio.Task] = set()
        self.html_active = 0
        self.html_peak = 0
        self.started_at = time.monotonic()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        return self

    async def __aexit__(self, *args):
        self.server.close()
        await self.server.wait_closed()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def url(self, host, path="/"):
        return f"http://{host}:{self.port}{path}"

    def client(self, **kwargs):
        kwargs.setdefault("timeout", 10)
        kwargs.setdefault("retry", 5)
        kwargs.setdefault("cf_bypass_url", self.base)
        kwargs.setdefault("log_fn", self.log)
        client = AsyncWebClient(**kwargs)
        client._session_kwargs["curl_options"] = {
            CurlOpt.RESOLVE: [f"{host}:{self.port}:127.0.0.1" for host in HOSTS],
            CurlOpt.PROXY: "",
        }
        return client

    def count(self, kind, host):
        return sum(e["kind"] == kind and e["host"] == host and e["event"] == "start" for e in self.events)

    def record(self, event, kind, host):
        entry = {
            "t": round(time.monotonic() - self.started_at, 3),
            "event": event,
            "kind": kind,
            "host": host,
            "html_active": self.html_active,
        }
        self.events.append(entry)
        if kind == "html":
            self.log(f"SERVER {entry}")

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        kind = host = ""
        try:
            raw = await reader.readuntil(b"\r\n\r\n")
            lines = raw.decode().split("\r\n")
            target = urlsplit(lines[0].split()[1])
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            headers = {k.lower(): v for k, v in headers.items()}
            if length := int(headers.get("content-length", "0")):
                await reader.readexactly(length)
            if target.path == "/html":
                kind = "html"
                host = urlsplit(parse_qs(target.query)["url"][0]).hostname
            elif "x-hostname" in headers:
                kind, host = "mirror", headers["x-hostname"]
            else:
                kind, host = "direct", headers["host"].split(":")[0]
            scenario = self.scenarios[host]
            if kind == "html":
                self.html_active += 1
                self.html_peak = max(self.html_peak, self.html_active)
            self.record("start", kind, host)
            if kind == "html":
                # Deliberately keep processing after disconnect, like the NAS service.
                await asyncio.sleep(scenario.html_delay)
                status, body = scenario.html_status, b"<html>ok</html>"
                if status == 200:
                    self.warmed.add(host)
            elif kind == "mirror":
                await asyncio.sleep(scenario.mirror_delay)
                status = 200 if host in self.warmed else scenario.mirror_status
                body = b"<html>ok</html>"
            else:
                await asyncio.sleep(scenario.direct_delay)
                status, body = scenario.direct_status, scenario.direct_body
            response = (
                f"HTTP/1.1 {status} Test\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
            ).encode() + body
            writer.write(response)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if kind == "html":
                self.html_active -= 1
                self.record("end", kind, host)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            self.tasks.discard(task)


class ProbeCrawler(BaseCrawler):
    @classmethod
    def site(cls):
        return Website.JAV321

    @classmethod
    def base_url_(cls):
        return "http://plain.test"

    async def _run(self, ctx):
        response, error = await self.async_client.request("GET", self.base_url + "/" + ctx.input.number)
        if response is None:
            ctx.debug(error)
            return None
        if response.content == b"no result":
            return None
        data = CrawlerResult.empty()
        data.title = "ok"
        return data

    async def _generate_search_url(self, ctx):
        return None

    async def _parse_search_page(self, ctx, html, search_url):
        return None

    async def _parse_detail_page(self, ctx, html, detail_url):
        return None
