"""独立线程里的常驻事件循环：承载扫描等后台任务。
不依赖请求所在事件循环（TestClient 每请求一个 loop），生产环境也与 API 循环解耦。
"""
import asyncio
import threading


class JobRunner:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None:
                return self._loop
            ready = threading.Event()

            def run() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._loop = loop
                ready.set()
                loop.run_forever()

            threading.Thread(target=run, daemon=True, name="ops-job-runner").start()
            ready.wait()
            return self._loop

    def submit(self, coro) -> "asyncio.Future":
        """把协程提交到后台循环执行，返回 concurrent.futures.Future。"""
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_started())


runner = JobRunner()
