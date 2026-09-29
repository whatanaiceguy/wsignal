import httpx
import pytest
from fastapi import FastAPI

from wsignal.interface.app import mount_web


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "index.html").write_text("<title>wsignal</title>")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    return tmp_path


@pytest.mark.asyncio
async def test_one_port_serves_the_app_its_files_and_leaves_api_404s_alone(dist):
    app = FastAPI()

    @app.get("/api/runs")
    async def runs():
        return []

    assert mount_web(app, dist)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        for path in ("/", "/runs/33", "/runs/33/e/5", "/dev/run/33"):
            page = await client.get(path)
            assert page.status_code == 200 and "<title>wsignal" in page.text
        assert (await client.get("/assets/app.js")).text == "console.log(1)"
        assert (await client.get("/favicon.svg")).text == "<svg/>"
        assert (await client.get("/api/runs")).json() == []
        assert (await client.get("/api/nope")).status_code == 404
        assert (await client.get("/health/x")).status_code == 404
        escape = await client.get("/..%2F..%2Fetc%2Fpasswd")
        assert "root:" not in escape.text


def test_without_a_build_nothing_is_mounted(tmp_path):
    assert mount_web(FastAPI(), tmp_path) is False
