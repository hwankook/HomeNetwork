import socket
import queue
import datetime
from typing import Union
from SocketThreads import *


class TCPClient:
    _name: str = 'TCPClient'
    _threadSend: Union[ThreadSend, None] = None
    _threadRecv: Union[ThreadRecv, None] = None
    _threadCheckRecvQueue: Union[ThreadCheckRecvQueue, None] = None

    def __init__(self, name: str = 'TCPClient', timeout: float = None, bufsize: int = 4096):
        """
        timeout: 0.0 = non-bloking mode, None = bloking mode, value = timeout mode
        """
        super().__init__()
        self._name = name
        self._sock = socket.socket()
        self._encoding = 'utf-8'
        self._timeout = timeout
        self._bufsize = bufsize
        self._queue_send = queue.Queue()
        self._queue_recv = queue.Queue()
        self._last_recv_time = datetime.datetime.now()

        self.sig_connected = Callback(bool)
        self.sig_disconnected = Callback()
        self.sig_send_data = Callback(bytes)
        self.sig_recv_data = Callback(bytes)
        self.sig_exception = Callback(str)

        self.startThreadSend()
        self.startThreadCheckRecvQueue()

    def __repr__(self) -> str:
        strinfo = '[%s][%x] ' % (type(self).__name__, id(self))
        if not self.isRunning():
            strinfo += 'Not Connected'
        else:
            servinfo = self.getServerInfo()
            myinfo = self.getClientInfo()
            strinfo += 'Connected to %s:(%d), %d' % (servinfo[0], servinfo[1], myinfo[1])
        return strinfo

    def __del__(self):
        self.release()
    
    def release(self):
        self.disconnect()
        try:
            del self._sock
        except AttributeError:
            pass
        self._sock = None
        self.stopThreadCheckRecvQueue()
        self.stopThreadSend()

    def initSocket(self, sock: socket.socket = None):
        if isinstance(self._sock, socket.socket):
            self._sock.close()
            del self._sock
            self._sock = None
        if sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        else:
            self._sock = sock
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(self._timeout)
        self.startThreadSend()
        if self._threadSend is not None:
            self._threadSend.setSocket(self._sock)

    def connect(self, server_addr: str, server_port: int = 80, disconnect: bool = False, **kwargs) -> bool:
        if disconnect:
            self.disconnect()
        elif self.isRunning():
            return
        success: bool = False

        try:
            self.initSocket()
            self.clearRecvQueue()
            self.clearSendQueue()
            if self._sock.gettimeout() is None:
                self._sock.settimeout(5)  # set connection timeout
                self._sock.connect((server_addr, server_port))
                self._sock.settimeout(None)
            else:
                self._sock.connect((server_addr, server_port))
            self.setKeepAlive()
            self.startThreadRecv()
            success = True
        except OSError as e:
            if e.args[0] == 10060:
                err_msg = f'No Server Response ({e})'
            elif e.args[0] == 10035:
                err_msg = f'Non-blocking socket operation error ({e})'
            else:
                err_msg = f'{e}'
            writeLog(err_msg, self)
            self.sig_exception.emit(err_msg)
        except Exception as e:
            writeLog(f'Unspecified exception ({e})', self)
            self.sig_exception.emit(str(e))

        if success:
            servinfo = self.getServerInfo()
            myinfo = self.getClientInfo()
            writeLog(f'"{self._name}" Connected (server={servinfo[0]}::{servinfo[1]}, self={myinfo[1]})', self)
            self._last_recv_time = datetime.datetime.now()
        else:
            try:
                if isinstance(self._sock, socket.socket):
                    self._sock.close()
            except Exception:
                pass
            self._sock = None
            writeLog('Failed to connect server', self)
        self.sig_connected.emit(success)
        return success

    def setKeepAlive(self, idle_sec: int = 10, interval_sec: int = 5, count: int = 3, user_timeout_ms: int = 30000):
        # EW11이 와이파이 재접속/재부팅 등으로 연결을 잃으면, 데이터를 받기만 하는 이쪽 소켓은
        # 끊긴 줄 모른 채 recv에서 영원히 대기한다 (half-open). TCP keepalive로 수십 초 안에 감지되게 한다
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt_name, value in [
                ('TCP_KEEPIDLE', idle_sec),
                ('TCP_KEEPINTVL', interval_sec),
                ('TCP_KEEPCNT', count),
                ('TCP_USER_TIMEOUT', user_timeout_ms)  # 응답 없는 전송 데이터도 이 시간 안에 실패 처리
            ]:
                opt = getattr(socket, opt_name, None)
                if opt is not None:
                    self._sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except Exception as e:
            writeLog(f'Failed to set TCP keepalive ({e})', self)

    def disconnect(self):
        self.stopThreadRecv()  # 먼저 종료 플래그를 세워서 깨어난 recv 스레드가 '연결 끊김'을 다시 보고하지 않게 한다
        if self.isRunning():
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError as e:
                writeLog(f'Shutdown Error- {e}', self)
                self.sig_exception.emit(str(e))
            except Exception as e:
                writeLog(f'Shutdown Exception- {e}', self)
                self.sig_exception.emit(str(e))
            try:
                self._sock.close()
            except OSError as e:
                writeLog(f'Close Error- {e}', self)
                self.sig_exception.emit(str(e))
            except Exception as e:
                writeLog(f'Close Exception- {e}', self)
                self.sig_exception.emit(str(e))
            writeLog('Disconnected', self)
        elif isinstance(self._sock, socket.socket):
            # peer reset 등으로 이미 끊긴 소켓 - 파일 디스크립터만 정리
            try:
                self._sock.close()
            except Exception:
                pass
        self.sig_disconnected.emit()

    def isRunning(self) -> bool:
        if not hasattr(self, '_sock'):
            return False
        if not isinstance(self._sock, socket.socket):
            return False
        if self.getServerInfo() is None:
           return False
        return True

    def isConnected(self) -> bool:
        return self.isRunning()

    def clearSendQueue(self):
        while not self._queue_send.empty():
            self._queue_send.get()

    def clearRecvQueue(self):
        while not self._queue_recv.empty():
            self._queue_recv.get()

    def getServerInfo(self):
        try:
            if self._sock is not None:
                peername = self._sock.getpeername()
                return peername
            else:
                return None
        except OSError as e:
            if e.args[0] == 10060:
                writeLog(f'getServerInfo Error- {e}', self)
                return None
            elif e.args[0] == 10057:
                writeLog(f'getServerInfo Error- {e}', self)
                return None
        except Exception as e:
            writeLog(f'getServerInfo Error- {e}', self)
            return None

    def getClientInfo(self):
        try:
            if self._sock is not None:
                sockname = self._sock.getsockname()
                return sockname
            else:
                return None
        except OSError as e:
            if e.args[0] == 10038:
                self.log('getClientInfo - OSError 10038')
                return None
        except Exception as e:
            writeLog(f'getClientInfo Error- {e}', self)
            return None

    def sendData(self, data: Union[bytes, bytearray, str]):
        if not self.isConnected():
            return
        try:
            if isinstance(data, str):
                sData = bytearray()
                sData.extend(map(ord, data))
                sData = bytes(sData)
                self._queue_send.put(sData)
            elif isinstance(data, bytes) or isinstance(data, bytearray):
                sData = bytes(data)
                self._queue_send.put(sData)
        except Exception as e:
            writeLog('Exception::{}'.format(e), self)
            self.sig_exception.emit(str(e))  

    def onRecvSomething(self, data: bytes):
        self._last_recv_time = datetime.datetime.now()

    def handleRecvData(self, data: bytes):
        self.sig_recv_data.emit(data)

    def handleSendData(self, data: bytes):
        self.sig_send_data.emit(data)

    def startThreadRecv(self):
        old = self._threadRecv
        if old is not None:
            if old.is_alive() and old._sock is self._sock and old._keepAlive:
                return
            # 재접속 직후 이전 연결의 recv 스레드가 아직 종료되지 않은 경우: 새 소켓용 스레드를 별도로 띄운다
            # (기존에는 이전 스레드가 남아 있으면 새 스레드를 만들지 않아 재접속 후 수신이 안 될 수 있었음)
            old.stop()
        thread = ThreadRecv(self._sock, self._queue_recv, self._bufsize)
        thread.sig_terminated.connect(lambda t=thread: self.onThreadRecvTerminated(t))
        thread.sig_exception.connect(lambda msg, term, t=thread: self.onThreadRecvException(t, msg, term))
        thread.sig_recv.connect(self.onRecvSomething)
        self._threadRecv = thread
        self._last_recv_time = datetime.datetime.now()
        thread.start()

    def stopThreadRecv(self):
        if self._threadRecv is not None:
            self._threadRecv.stop()

    def onThreadRecvTerminated(self, thread: ThreadRecv = None):
        if thread is None or thread is self._threadRecv:
            self._threadRecv = None

    def onThreadRecvException(self, thread: ThreadRecv, message: str, terminate: bool):
        if thread is not self._threadRecv or thread._sock is not self._sock:
            # 이전 연결의 스레드가 늦게 보고한 예외 - 새로 맺은 연결을 끊지 않도록 무시
            writeLog(f'Ignore exception from previous connection ({message})', self)
            return
        self.onThreadException(message, terminate)

    def startThreadCheckRecvQueue(self):
        if self._threadCheckRecvQueue is None:
            self._threadCheckRecvQueue = ThreadCheckRecvQueue(self._queue_recv)
            self._threadCheckRecvQueue.sig_terminated.connect(self.onThreadCheckRecvQueueTerminated)
            self._threadCheckRecvQueue.sig_exception.connect(self.onThreadException)
            self._threadCheckRecvQueue.sig_recv.connect(self.handleRecvData)
            self._threadCheckRecvQueue.start()

    def stopThreadCheckRecvQueue(self):
        if self._threadCheckRecvQueue is not None:
            self._threadCheckRecvQueue.stop()

    def onThreadCheckRecvQueueTerminated(self):
        del self._threadCheckRecvQueue
        self._threadCheckRecvQueue = None

    def startThreadSend(self):
        if self._threadSend is None:
            self._threadSend = ThreadSend(self._sock, self._queue_send)
            self._threadSend.sig_terminated.connect(self.onThreadSendTerminated)
            self._threadSend.sig_exception.connect(self.onThreadException)
            self._threadSend.sig_send.connect(self.handleSendData)
            self._threadSend.start()

    def stopThreadSend(self):
        if self._threadSend is not None:
            self._threadSend.stop()

    def onThreadSendTerminated(self):
        del self._threadSend
        self._threadSend = None

    def onThreadException(self, message: str, terminate: bool):
        writeLog(f'Error Occurred::{message}', self)
        self.sig_exception.emit(message)
        if terminate:
            self.disconnect()
    
    def time_after_last_recv(self) -> float:
        delta = datetime.datetime.now() - self._last_recv_time
        return delta.total_seconds()

    @property
    def name(self) -> str:
        return self._name


if __name__ == '__main__':
    import time

    client = TCPClient()
    client.connect('192.168.0.97', 8899)
    time.sleep(10)
    client.disconnect()
    client.release()
