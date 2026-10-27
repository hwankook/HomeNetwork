import os
import sys
import time
import threading
import traceback
from typing import List
CURPATH = os.path.dirname(os.path.abspath(__file__))  # Project/Include/Threads
INCPATH = os.path.dirname(CURPATH)  # Project/Include/
PROJPATH = os.path.dirname(INCPATH)  # Proejct/
RS485PATH = os.path.join(PROJPATH, 'RS485')  # Project/RS485
sys.path.extend([CURPATH, RS485PATH, INCPATH])
sys.path = list(set(sys.path))
del CURPATH, INCPATH, PROJPATH, RS485PATH
from Common import Callback, writeLog
from RS485 import RS485Comm


class ThreadTimer(threading.Thread):
    _keepAlive: bool = True
    _publish_count: int = 0
    _home_initialized: bool = False

    def __init__(
        self,
        rs485_list: List[RS485Comm],
        publish_interval: int = 60,
        interval_ms: int = 2000,
        check_idle_sec: int = 30,
        reconnect_limit_sec: int = 60,
        verbose_regular_publish: dict = None,
        idle_check_list: List[RS485Comm] = None,
        reconnect_interval_sec: float = 10.
    ):
        threading.Thread.__init__(self, name='Timer Thread')
        self._rs485_list = rs485_list
        self._publish_interval = publish_interval  # 단위: 초
        self._interval_ms = interval_ms  # 단위: 밀리초
        self._check_idle_sec = check_idle_sec
        self._reconnect_limit_sec = reconnect_limit_sec
        self._verbose_regular_publish = verbose_regular_publish
        # idle(수신 없음) 감시 대상 - None이면 기존 동작대로 전체
        self._idle_check_list = list(rs485_list) if idle_check_list is None else list(idle_check_list)
        self._reconnect_interval_sec = reconnect_interval_sec  # 재접속 시도 최소 간격
        self._last_reconnect_try = dict()  # id(obj) -> perf_counter
        self._last_warn = dict()  # id(obj) -> perf_counter (경고 로그 도배 방지)
        self.sig_terminated = Callback()
        self.sig_publish_regular = Callback()

    def run(self):
        first_publish: bool = False
        time.sleep(2)  # wait for initialization
        writeLog('Started', self)
        tm_publish = time.perf_counter()
        tm_loop = time.perf_counter_ns() / 1e6
        while self._keepAlive:
            try:
                if not self._home_initialized:
                    writeLog('Waiting for Initializing Home...', self)
                    time.sleep(1)
                    continue

                if time.perf_counter_ns() / 1e6 - tm_loop > self._interval_ms:
                    tm_loop = time.perf_counter_ns() / 1e6
                    rs485_all_connected: bool = sum([x.isConnected() for x in self._rs485_list]) == len(self._rs485_list)
                    if rs485_all_connected and not first_publish:
                        first_publish = True
                        writeLog('RS485 are all opened >> Publish', self)
                        self.sig_publish_regular.emit()
                        tm_publish = time.perf_counter()
                    self.check_rs485_status()

                if time.perf_counter() - tm_publish >= self._publish_interval:
                    self.sig_publish_regular.emit()
                    self._publish_count += 1
                    if self._verbose_regular_publish is not None:
                        enable = self._verbose_regular_publish.get('enable')
                        interval = self._verbose_regular_publish.get('interval')
                        if enable and self._publish_count % interval == 0:
                            writeLog(f'Regular Publishing Device State MQTT (#: {self._publish_count}, interval: {self._publish_interval} sec)', self)
                    tm_publish = time.perf_counter()
                
                time.sleep(100e-3)
            except Exception as e:
                writeLog(f'Exception::{e}', self)
                traceback.print_exc()
        writeLog('Terminated', self)
        self.sig_terminated.emit()

    def stop(self):
        self._keepAlive = False
    
    def setMqttPublishInterval(self, interval: int):
        self._publish_interval = interval
        writeLog(f'Set Regular MQTT Publish Interval as {self._publish_interval} sec', self)

    def set_home_initialized(self):
        self._home_initialized = True
    
    def _warn_throttled(self, obj, msg: str, interval: float = 10.):
        now = time.perf_counter()
        if now - self._last_warn.get(id(obj), 0.) >= interval:
            self._last_warn[id(obj)] = now
            writeLog(msg, self)

    def _try_reconnect(self, obj, reason: str):
        now = time.perf_counter()
        if now - self._last_reconnect_try.get(id(obj), 0.) < self._reconnect_interval_sec:
            return
        self._last_reconnect_try[id(obj)] = now
        writeLog('Try to reconnect RS485 <{}> ({})'.format(obj.name, reason), self)
        obj.reconnect()
        if obj.isConnected():
            writeLog('RS485 <{}> reconnected'.format(obj.name), self)

    def check_rs485_status(self) -> bool:
        result = True
        for obj in self._rs485_list:
            if obj.isConnected():
                if obj not in self._idle_check_list:
                    continue
                delta = obj.time_after_last_recv()
                if delta > self._check_idle_sec:
                    self._warn_throttled(obj, 'Warning!! RS485 <{}> is not receiving for {:.1f} seconds'.format(obj.name, delta))
                    if delta > self._reconnect_limit_sec:
                        result = False
                        # 일정 시간 이상 패킷을 받지 못하면 재접속 시도
                        self._try_reconnect(obj, 'no packet for {:.0f} sec'.format(delta))
            else:
                result = False
                self._warn_throttled(obj, 'Warning!! RS485 <{}> is not connected'.format(obj.name))
                self._try_reconnect(obj, 'disconnected')
        return result
