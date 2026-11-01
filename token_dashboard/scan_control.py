import threading


class ScanBusy(RuntimeError):
    pass


class ScanCoordinator:
    def __init__(self, cleanup):
        self._lock = threading.Lock()
        self._cleanup = cleanup

    def _execute(self, operation):
        try:
            return operation()
        finally:
            try:
                self._cleanup()
            finally:
                self._lock.release()

    def run(self, operation):
        if not self._lock.acquire(blocking=False):
            raise ScanBusy("scan already running")
        return self._execute(operation)

    def start(self, operation):
        if not self._lock.acquire(blocking=False):
            raise ScanBusy("scan already running")
        try:
            worker = threading.Thread(target=self._execute, args=(operation,), daemon=True)
            worker.start()
        except BaseException:
            self._lock.release()
            raise
        return worker
