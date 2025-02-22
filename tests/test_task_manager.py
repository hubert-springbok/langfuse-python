import logging
import subprocess
import threading
from urllib.parse import urlparse, urlunparse
import httpx

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from langfuse.request import LangfuseClient
from langfuse.task_manager import TaskManager

logging.basicConfig()
log = logging.getLogger("langfuse")
log.setLevel(logging.DEBUG)


def setup_server(httpserver, expected_body: dict):
    httpserver.expect_request(
        "/api/public/ingestion", method="POST", json=expected_body
    ).respond_with_data("success")


def setup_langfuse_client(server: str):
    return LangfuseClient(
        "public_key", "secret_key", server, "1.0.0", 15, httpx.Client()
    )


def get_host(url):
    parsed_url = urlparse(url)
    new_url = urlunparse((parsed_url.scheme, parsed_url.netloc, "", "", "", ""))
    return new_url


@pytest.mark.timeout(10)
def test_multiple_tasks_without_predecessor(httpserver: HTTPServer):
    failed = False

    def handler(request: Request):
        try:
            if request.json["batch"][0]["foo"] == "bar":
                return Response(status=200)
            return Response(status=500)
        except Exception as e:
            print(e)
            logging.error(e)
            nonlocal failed
            failed = True

    httpserver.expect_request(
        "/api/public/ingestion", method="POST"
    ).respond_with_handler(handler)

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    tm = TaskManager(
        langfuse_client, 10, 0.1, 3, 1, 10_000, "test-sdk", "1.0.0", "default"
    )

    tm.add_task({"foo": "bar"})
    tm.add_task({"foo": "bar"})
    tm.add_task({"foo": "bar"})

    tm.flush()
    assert not failed


@pytest.mark.timeout(10)
def test_task_manager_fail(httpserver: HTTPServer):
    count = 0

    def handler(request: Request):
        nonlocal count
        count = count + 1
        return Response(status=500)

    httpserver.expect_request(
        "/api/public/ingestion", method="POST"
    ).respond_with_handler(handler)

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    tm = TaskManager(
        langfuse_client, 10, 0.1, 3, 1, 10_000, "test-sdk", "1.0.0", "default"
    )

    tm.add_task({"foo": "bar"})
    tm.flush()

    assert count == 3


@pytest.mark.timeout(20)
def test_consumer_restart(httpserver: HTTPServer):
    failed = False

    def handler(request: Request):
        try:
            if request.json["batch"][0]["foo"] == "bar":
                return Response(status=200)
            return Response(status=500)
        except Exception as e:
            print(e)
            logging.error(e)
            nonlocal failed
            failed = True

    httpserver.expect_request(
        "/api/public/ingestion", method="POST"
    ).respond_with_handler(handler)

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    tm = TaskManager(
        langfuse_client, 10, 0.1, 3, 1, 10_000, "test-sdk", "1.0.0", "default"
    )

    tm.add_task({"foo": "bar"})
    tm.flush()

    tm.add_task({"foo": "bar"})
    tm.flush()
    assert not failed


@pytest.mark.timeout(10)
def test_concurrent_task_additions(httpserver: HTTPServer):
    counter = 0

    def handler(request: Request):
        nonlocal counter
        counter = counter + 1
        return Response(status=200)

    def add_task_concurrently(tm, func):
        tm.add_task(func)

    httpserver.expect_request(
        "/api/public/ingestion", method="POST"
    ).respond_with_handler(handler)

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    tm = TaskManager(
        langfuse_client, 1, 0.1, 3, 1, 10_000, "test-sdk", "1.0.0", "default"
    )
    threads = [
        threading.Thread(target=add_task_concurrently, args=(tm, {"foo": "bar"}))
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tm.shutdown()

    assert counter == 10


@pytest.mark.timeout(10)
def test_atexit():
    python_code = """
import time
import logging
from langfuse.task_manager import TaskManager  # assuming task_manager is the module name
from langfuse.request import LangfuseClient
import httpx

langfuse_client = LangfuseClient("public_key", "secret_key", "http://localhost:3000", "1.0.0", 15, httpx.Client())

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler()
    ]
)
print("Adding task manager", TaskManager)
manager = TaskManager(langfuse_client, 10, 0.1, 3, 1, 10_000, "test-sdk", "1.0.0", "default")

"""

    process = subprocess.Popen(
        ["python", "-c", python_code], stderr=subprocess.PIPE, text=True
    )

    logs = ""

    try:
        for line in process.stderr:
            logs += line.strip()
            print(line.strip())
    except subprocess.TimeoutExpired:
        pytest.fail("The process took too long to execute")
    process.communicate()

    returncode = process.returncode
    if returncode != 0:
        pytest.fail("Process returned with error code")

    print(process.stderr)

    assert "consumer thread 0 joined" in logs


def test_flush(httpserver: HTTPServer):
    # set up the consumer with more requests than a single batch will allow

    failed = False

    def handler(request: Request):
        try:
            if request.json["batch"][0]["foo"] == "bar":
                return Response(status=200)
            return Response(status=500)
        except Exception as e:
            print(e)
            logging.error(e)
            nonlocal failed
            failed = True

    httpserver.expect_request(
        "/api/public/ingestion",
        method="POST",
    ).respond_with_handler(handler)

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    tm = TaskManager(
        langfuse_client, 1, 0.1, 3, 1, 10_000, "test-sdk", "1.0.0", "default"
    )

    for _ in range(100):
        tm.add_task({"foo": "bar"})
    # We can't reliably assert that the queue is non-empty here; that's
    # a race condition. We do our best to load it up though.
    tm.flush()
    # Make sure that the client queue is empty after flushing
    assert tm._queue.empty()
    assert not failed


def test_shutdown(httpserver: HTTPServer):
    # set up the consumer with more requests than a single batch will allow

    failed = False

    def handler(request: Request):
        try:
            if request.json["batch"][0]["foo"] == "bar":
                return Response(status=200)
            return Response(status=500)
        except Exception as e:
            print(e)
            logging.error(e)
            nonlocal failed
            failed = True

    httpserver.expect_request(
        "/api/public/ingestion",
        method="POST",
    ).respond_with_handler(handler)

    langfuse_client = setup_langfuse_client(
        get_host(httpserver.url_for("/api/public/ingestion"))
    )

    tm = TaskManager(
        langfuse_client, 1, 0.1, 3, 5, 10_000, "test-sdk", "1.0.0", "default"
    )

    for _ in range(100):
        tm.add_task({"foo": "bar"})

    tm.shutdown()
    # we expect two things after shutdown:
    # 1. client queue is empty
    # 2. consumer thread has stopped
    assert tm._queue.empty()

    assert len(tm._consumers) == 5
    for c in tm._consumers:
        assert not c.is_alive()
    assert tm._queue.empty()
    assert not failed
