import os
import sys
import time
import queue
import socket
import threading
from typing import Union
from abc import abstractmethod, ABCMeta
CURPATH = os.path.dirname(os.path.abspath(__file__))  # {$PROJECT}/Include/RS485/Socket
INCPATH = os.path.dirname(os.path.dirname(CURPATH))  # {$PROJECT}/Include/
sys.path.extend([CURPATH, INCPATH])
sys.path = list(set(sys.path))
del CURPATH, INCPATH
from Common import writeLog, Callback


class ThreadCommon(threading.Thread):
    __metaclass__ = ABCMeta

    _keepAlive: bool = True
    _sock: Union[socket.socket, None] = None
    _loop_sleep_time: float = 1e-3

    def __init__(self, name: str = 'Socket Thread Common'):
        threading.Thread.__init__(self, name=name)
        self.sig_terminated = Callback()
        self.sig_send = Callback(bytes)
        self.sig_recv = Callback(bytes)
        self.sig_exception = Callback(str, bool)  # message, terminate socket flag
        self.setDaemon(True)
    
    def run(self):
        writeLog('Started', self)
        while self._keepAlive:
            self.loop()
            if self._loop_sleep_time > 0:
                time.sleep(self._loop_sleep_time)
        writeLog('Terminated', self)
        self.sig_terminated.emit()
    
    @abstractmethod
    def loop(self):
        pass

    def stop(self):
        self._keepAlive = False
    
    def setSocket(self, sock: socket.socket):
        self._sock = sock


class ThreadSend(ThreadCommon):
    def __init__(self, sock: socket.socket, queue_send: queue.Queue):
        super().__init__(name='Socket Thread Send')
        self._sock = sock
        self._queue_send = queue_send
    
    def loop(self):
        try:
            if not isinstance(self._sock, socket.socket):
                return
            if not self._queue_send.empty():
                data = self._queue_send.get()
                datalen = len(data)
                while datalen > 0:
                    sendlen = self._sock.send(data)
                    self.sig_send.emit(data[:sendlen])
                    data = data[sendlen:]
                    datalen = len(data)
        except OSError as e:
            # 리눅스에서는 EPIPE(32), ECONNRESET(104), ETIMEDOUT(110) 등이 발생 - 윈도우 코드(10038)만 처리하면
            # 전송 실패가 조용히 무시되어 연결이 끊긴 것을 알 수 없다
            self.sig_exception.emit(f'Send failed ({e})', True)
        except Exception as e:
            self.sig_exception.emit(f'Exception ({e})', True)


class ThreadRecv(ThreadCommon):
    def __init__(self, sock: socket.socket, queue_recv: queue.Queue, bufsize: int = 4096):
        super().__init__(name='Socket Thread Recv')
        self._sock = sock
        self._queue_recv = queue_recv
        self._bufsize = bufsize
        self._loop_sleep_time = 0.

    def loop(self):
        try:
            if isinstance(self._sock, socket.socket):
                data = self._sock.recv(self._bufsize)
                if data is None or len(data) == 0:
                    if self._keepAlive:  # 의도적으로 종료(stop)한 경우는 알리지 않음
                        self.sig_exception.emit('Lost connection', True)
                    self.stop()
                else:
                    self.sig_recv.emit(data)
                    self._queue_recv.put(data)
            else:
                time.sleep(0.1)
        except socket.timeout:
            pass
        except OSError as e:
            if e.args[0] == 10022:  # (windows) 아직 연결되지 않은 소켓
                self.sig_exception.emit(str(e), False)
                time.sleep(0.1)
                return
            # 리눅스: ECONNRESET(104), ETIMEDOUT(110, keepalive 실패), EBADF(9) 등
            # 기존에는 윈도우 에러코드만 처리해서 이 경우 예외를 삼킨 채 recv를 무한 반복했다
            if self._keepAlive:
                self.sig_exception.emit(f'Lost connection ({e})', True)
            self.stop()
        except Exception as e:
            if self._keepAlive:
                self.sig_exception.emit(f'Exception ({e})', True)
            self.stop()


class ThreadCheckRecvQueue(ThreadCommon):
    def __init__(self, queue_recv: queue.Queue):
        super().__init__(name='Socket Thread Check Recv Queue')
        self._queue_recv = queue_recv
    
    def loop(self):
        if not self._queue_recv.empty():
            data = self._queue_recv.get()
            self.sig_recv.emit(data)
