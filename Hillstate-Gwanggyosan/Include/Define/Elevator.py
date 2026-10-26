import json
from Device import *
from enum import IntEnum


class CommandState(IntEnum):
    IDLE = 0
    ACCEPTED = 1  # 일부 단지: 이동 중 상태(A*/B*)의 하위 4비트가 1로 들어오는 경우
    UPSIDE = 5
    DOWNSIDE = 6
    DOWNSIDE2 = 7  # Imazu (HNT-4105)?


class MovingState(IntEnum):
    IDLE = 0
    ARRIVED = 1
    MOVINGUP = 5
    MOVINGDOWN = 6


class CheckCommandMethod(IntEnum):
    CALL_STATE = 0
    DEV_CMD_STATE = 1


class PacketCallType(IntEnum):
    COMMAND = 0  # F7 0B 01 34 02 41 10 XX 00 YY EE (월패드 명령 형식)
    RESPONSE = 1  # F7 0B 01 34 04 41 10 00 XX YY EE (복도 미니패드 응답 형식)
    AUTO = 2  # 0, 1 형식을 번갈아 전송


class DevInfo:
    index: int  # n호기
    command_state: CommandState
    moving_state: MovingState
    floor: str
    floor_prev: str

    def __init__(self, index: int):
        self.index = index
        self.command_state = CommandState.IDLE
        self.moving_state = MovingState.IDLE
        self.floor = ''
        self.floor_prev = ''

    def __repr__(self) -> str:
        return f'<Elevator#{self.index}: Command State: {self.command_state.name}, Moving State:{self.moving_state.name}, FLOOR:{self.floor}>'


class ThreadStateChangeTimer(threading.Thread):
    _keepAlive: bool = True

    def __init__(self, dev: Device, change_time: float):
        threading.Thread.__init__(self, name=f'Device({dev}) State Change Timer')
        self._change_time = change_time
        self._tick_start: float = 0.
        self.sig_action = Callback()
        self.sig_terminated = Callback()
    
    def run(self):
        writeLog(f'{self.name} Started', self)
        self._tick_start = time.perf_counter()
        while self._keepAlive:
            elapsed = time.perf_counter() - self._tick_start
            if elapsed >= self._change_time:
                self.sig_action.emit()
                break
            time.sleep(100e-3)
        writeLog(f'{self.name} Terminated', self)
        self.sig_terminated.emit()
    
    def stop(self):
        self._keepAlive = False
    
    def reset(self):
        self._tick_start = time.perf_counter()


class Elevator(Device):
    time_arrived: float = 0.
    time_threshold_arrived_change: float = 10.
    dev_info_list: List[DevInfo]
    ha_dev_config_list: List[dict]
    ready_to_clear: bool = True

    time_call_started: float = 0.
    time_threshold_check_duration: float = 10.

    packet_call_type: int = 0
    packet_command_call_down_value: int = 6  # 하행 호출 명령값 (default = 6, imazu 7인 경우가 있음?)

    state_call: int = 0  # 미니월패드 호출중 상태, 0 = idle, 5 = 상행호출중(미지원), 6 = 하행호출중
    state_call_prev: int = 0
    arrived_flag: bool = False

    verbose_packet: bool = False

    check_command_method: CheckCommandMethod = CheckCommandMethod.CALL_STATE

    # 상태 고착 방지용 타임스탬프 (time.perf_counter 기준)
    time_last_response: float = 0.  # 마지막 미니패드 응답(04) 패킷 수신 시각
    time_call_state_changed: float = 0.  # state_call 값이 마지막으로 바뀐 시각
    time_dev_cmd_changed: float = 0.  # 엘리베이터 호기별 command state가 마지막으로 바뀐 시각
    time_state_changed: float = 0.  # state 값이 마지막으로 바뀐 시각
    call_state_stale_sec: float = 60.  # 응답 패킷 없이 이 시간이 지나면 호출 상태(state_call)를 idle로 간주
    state_stale_sec: float = 300.  # state(호출중/도착)가 이 시간 이상 그대로면 idle로 복귀

    _thread_state_change_timer: Union[ThreadStateChangeTimer, None] = None

    def __init__(self, name: str = 'Elevator', index: int = 0, room_index: int = 0, topic_prefix: str = 'home'):
        super().__init__(name, index, room_index, topic_prefix)
        self.dev_type = DeviceType.ELEVATOR
        self.unique_id = f'elevator_{self.room_index}_{self.index}'
        self.mqtt_state_topic = f'{topic_prefix}/state/elevator/{self.room_index}/{self.index}'
        self.mqtt_command_topic = f'{topic_prefix}/command/elevator/{self.room_index}/{self.index}'
        self.dev_info_list = list()
        self.ha_dev_config_list = list()
    
    def setDefaultName(self):
        self.name = 'Elevator'

    def setPacketCallType(self, value: int):
        self.packet_call_type = value
    
    def getPacketCallType(self) -> int:
        return self.packet_call_type

    def setPacketCommandCallDownValue(self, value: int):
        self.packet_command_call_down_value = value
    
    def getPacketCommandCallDownValue(self) -> int:
        return self.packet_command_call_down_value

    def setVerbosePacket(self, value: bool):
        self.verbose_packet = value

    def getVerbosePacket(self) -> bool:
        return self.verbose_packet

    def setCheckCommandMethod(self, value: int):
        try:
            self.check_command_method = CheckCommandMethod(value)
        except Exception:
            self.check_command_method = CheckCommandMethod.CALL_STATE

    def publishMQTT(self):
        if self.mqtt_client is None:
            return
        obj = {
            "state": self.state if not self.arrived_flag else 1
        }
        self.mqtt_client.publish(self.mqtt_state_topic, json.dumps(obj), 1)
        self.publishMQTTDevInfo()
        # writeLog(f"pub <{self.mqtt_state_topic}>: {obj}", self)
    
    def publishMQTTDevInfo(self):
        for elem in self.ha_dev_config_list:
            ev_index = elem.get('index')
            dev_find = list(filter(lambda x: x.index == ev_index, self.dev_info_list))
            if len(dev_find) > 0:
                dev = dev_find[0]
                moving_state = dev.moving_state
                direction = moving_state.name if moving_state in [MovingState.MOVINGUP, MovingState.MOVINGDOWN] else ""
                floor = dev.floor if moving_state in [MovingState.MOVINGUP, MovingState.MOVINGDOWN] else ""
                obj = {
                    "direction": direction.replace("MOVING", ""),
                    "floor": floor
                }
            else:
                obj = {
                    "direction": "",
                    "floor": ""
                }
            topic = self.mqtt_state_topic + f'/dev/{ev_index}'
            self.mqtt_client.publish(topic, json.dumps(obj), 1)
            # writeLog(f"pub <{topic}>: {obj}", self)

    def configMQTT(self, retain: bool = False):
        if self.mqtt_client is None:
            return
        
        # 호출 스위치 및 도착 알림용 센서를 위해 디바이스 정보를 각각 발행해야 한다
        topic = f'{self.ha_discovery_prefix}/switch/{self.unique_id}_calldown/config'
        # payload_on_template = '{ "state": ' + f'{self.packet_command_call_down_value}' + ' }'
        payload_on_template = '{ "state": 6 }'
        obj = {
            "name": self.name + " Call (Down)",
            "unique_id": self.unique_id + "_calldown",
            "state_topic": self.mqtt_state_topic,
            "command_topic": self.mqtt_command_topic,
            "value_template": '{ "state": {{ value_json.state }} }',
            "payload_on": payload_on_template,
            "payload_off": '{ "state": 0 }',
            "icon": "mdi:elevator"
        }
        if self.check_ha_core_version("2025.10.1"):
            obj["default_entity_id"] = self.unique_id + "_calldown"
        else:
            obj["object_id"] = self.unique_id + "_calldown"
        self.mqtt_client.publish(topic, json.dumps(obj), 1, retain)

        topic = f'{self.ha_discovery_prefix}/sensor/{self.unique_id}_arrived/config'
        obj = {
            "name": self.name + " Arrived",
            "unique_id": self.unique_id + "_arrived",
            "state_topic": self.mqtt_state_topic,
            "value_template": "{% if value_json.state == 0 %} \
                               IDLE \
                               {% elif value_json.state == 1 %} \
                               ARRIVED \
                               {% else %} \
                               MOVING \
                               {% endif %}",
            "icon": "mdi:elevator-passenger"
        }
        if self.check_ha_core_version("2025.10.1"):
            obj["default_entity_id"] = self.unique_id + "_arrived"
        else:
            obj["object_id"] = self.unique_id + "_arrived"
        self.mqtt_client.publish(topic, json.dumps(obj), 1, retain)

        # add homebridge accessory
        name = self.name + " CALL(DOWN)"
        hb_config = self.read_homebridge_config_template()
        accessories = hb_config.get('accessories')
        find = list(filter(lambda x: x.get('name') == name, accessories))
        if len(find) > 0:
            return
        
        elem = {
            "name": name,
            "accessory": "mqttthing",
            "type": "switch",
            "url": f"{self.mqtt_host}:{self.mqtt_port}",
            "username": self.mqtt_username,
            "password": self.mqtt_password,
            "integerValue": True,
            "onValue": 1, 
            "offValue": 0,
            "history": True,
            "logMqtt": False,
            "topics": {
                "getOn": {
                    "topic": self.mqtt_state_topic,
                    "apply": "return (JSON.parse(message).state == 6);"
                },
                "setOn": {
                    "topic": self.mqtt_command_topic,
                    "apply": "return JSON.stringify({state: 6});"
                }
            }
        }
        accessories.append(elem)

        elem = {
            "name": self.name + " ARRIVED",
            "accessory": "mqttthing",
            "type": "occupancySensor",
            "url": f"{self.mqtt_host}:{self.mqtt_port}",
            "username": self.mqtt_username,
            "password": self.mqtt_password,
            "integerValue": True,
            "onValue": 1, 
            "offValue": 0,
            "history": True,
            "logMqtt": False,
            "topics": {
                "getOccupancyDetected": {
                    "topic": self.mqtt_state_topic,
                    "apply": "return (JSON.parse(message).state == 1);"
                }
            }
        }
        accessories.append(elem)

        self.write_homebridge_config_template(hb_config)

    def configMQTTDevInfo(self, ev_dev_idx: int, retain: bool = False):
        if self.mqtt_client is None:
            return
        
        find = list(filter(lambda x: x.get('index') == ev_dev_idx, self.ha_dev_config_list))
        if len(find) == 0:
            ev_info = {
                'index': ev_dev_idx,
                'config': False
            }
            self.ha_dev_config_list.append(ev_info)
        else:
            ev_info = find[0]

        if not ev_info.get('config', False):
            topic = f'{self.ha_discovery_prefix}/sensor/{self.unique_id}_{ev_dev_idx}_floor/config'
            obj = {
                "name": self.name + f" #{ev_dev_idx} Floor",
                "unique_id": self.unique_id + f"_{ev_dev_idx}_floor",
                "state_topic": self.mqtt_state_topic + f'/dev/{ev_dev_idx}',
                "value_template": "{{ value_json.floor }}",
                "icon": "mdi:counter"
            }
            if self.check_ha_core_version("2025.10.1"):
                obj["default_entity_id"] = self.unique_id + f"_{ev_dev_idx}_floor"
            else:
                obj["object_id"] = self.unique_id + f"_{ev_dev_idx}_floor"
            self.mqtt_client.publish(topic, json.dumps(obj), 1, retain)

            topic = f'{self.ha_discovery_prefix}/sensor/{self.unique_id}_{ev_dev_idx}_direction/config'
            obj = {
                "name": self.name + f" #{ev_dev_idx} Direction",
                "unique_id": self.unique_id + f"_{ev_dev_idx}_direction",
                "state_topic": self.mqtt_state_topic + f'/dev/{ev_dev_idx}',
                "value_template": "{{ value_json.direction }}",
                "icon": "mdi:swap-vertical-bold"
            }
            if self.check_ha_core_version("2025.10.1"):
                obj["default_entity_id"] = self.unique_id + f"_{ev_dev_idx}_direction"
            else:
                obj["object_id"] = self.unique_id + f"_{ev_dev_idx}_direction"
            self.mqtt_client.publish(topic, json.dumps(obj), 1, retain)

            ev_info['config'] = True
            writeLog(f"EV#{ev_dev_idx} HA entity configured", self)

    def updateState(self, _: int, **kwargs):
        data_type = kwargs.get('data_type')
        if data_type == 'query':
            raw_command_state = kwargs.get('command_state', 0)
            try:
                command_state = CommandState(raw_command_state)  # possible values: 0(idle), 1, 5(command up), 6/7(command down)
            except ValueError:
                writeLog(f"unknown command state value ({raw_command_state}), packet: {kwargs.get('packet')}", self)
                command_state = CommandState.ACCEPTED
            raw_moving_state = kwargs.get('moving_state', 0)
            try:
                moving_state = MovingState(raw_moving_state)  # possible values: 0(idle), 1(arrived), 5(moving upside), 6(moving downside)
            except ValueError:
                writeLog(f"unknown moving state value ({raw_moving_state}), packet: {kwargs.get('packet')}", self)
                moving_state = MovingState.IDLE
            ev_dev_idx = kwargs.get('ev_dev_idx', 0)
            floor = kwargs.get('floor', '')
            if self.verbose_packet:
                packet = kwargs.get('packet')
                writeLog(f"[Q] command: {command_state.name}, moving: {moving_state.name}, index: {ev_dev_idx}, floor: {floor}, packet: {packet}")
            if command_state != CommandState.IDLE:
                # if CommandState(self.state_call) == command_state:
                #    self.state = self.state_call

                find = list(filter(lambda x: x.index == ev_dev_idx, self.dev_info_list))
                if len(find) == 0:
                    dev_info = DevInfo(ev_dev_idx)
                    writeLog(f"EV#{ev_dev_idx} object is not in list, appending", self)
                    self.dev_info_list.append(dev_info)
                    self.dev_info_list.sort(key=lambda x: x.index)
                else:
                    dev_info = find[0]
                if dev_info.command_state != command_state:
                    self.time_dev_cmd_changed = time.perf_counter()
                dev_info.command_state = command_state
                dev_info.moving_state = moving_state
                dev_info.floor = floor
                self.configMQTTDevInfo(ev_dev_idx, True)  # HA Config MQTT Each Elevators
                self.publishMQTTDevInfo()
            else:
                if moving_state == MovingState.IDLE:
                    if CommandState(self.state_call) == command_state:
                        self.state = 0
                elif moving_state == MovingState.ARRIVED:
                    self.state = 1
                for dev in self.dev_info_list:
                    dev.command_state = CommandState.IDLE
                    dev.moving_state = MovingState.IDLE
                    dev.floor = ''
                self.publishMQTTDevInfo()
        elif data_type == 'response':
            self.state_call = kwargs.get('call_state', 0)
            self.time_last_response = time.perf_counter()
            if self.state_call != self.state_call_prev:
                self.time_call_state_changed = self.time_last_response
            if self.verbose_packet:
                packet = kwargs.get('packet')
                writeLog(f"[R] call: {self.state_call}, packet: {packet}")
            if self.state_call != self.state_call_prev:
                if self.state_call:
                    if self.state_call == self.packet_command_call_down_value:
                        self.state = int(CommandState.DOWNSIDE)  # HA 스위치(payload_on: 6)와 맞추기 위해 정규화
                    else:
                        self.state = self.state_call
                    writeLog(f"Started calling ({self.state_call})", self)
                    self.time_call_started = time.perf_counter()
                else:
                    # 0 = IDLE
                    writeLog(f"Finished calling", self)
            self.state_call_prev = self.state_call
        
        # console log current floor
        ev_dev_info_str = ''
        diff_floor = False
        for dev in self.dev_info_list:
            if dev.command_state is not CommandState.IDLE:
                    ev_dev_info_str += f'EV#{dev.index} Floor: {dev.floor} ({dev.moving_state.name}), '
            if dev.floor != dev.floor_prev:
                diff_floor = True
            dev.floor_prev = dev.floor
        if len(ev_dev_info_str) > 0 and diff_floor:
            writeLog(ev_dev_info_str[:-2], self)

        if self.state != self.state_prev:
            writeLog(f"State changed from {self.state_prev} to {self.state}", self)
            self.time_state_changed = time.perf_counter()
            self.arrived_flag = False
            if self.state_prev == 0:
                if self.state in [5, 6]:
                    self.publishMQTT()
                elif self.state == 1:
                    # 미니패드가 없는 경우, 엘리베이터의 현재 상태가 5/6으로 바뀌지 못한다 (월패드-미니패드간 통신 없음)
                    # 주기적인 패킷 파싱이 불가능하기 때문에 우선 arrived 상태임을 publish한 뒤, 일정 시간(타이머) 뒤에
                    # idle 상태를 publish하게 임시조치
                    self.publishMQTT()
                    self.startThreadStateChangeTimer()
            elif self.state_prev in [5, 6]:
                if self.state == 1:
                    self.time_arrived = time.perf_counter()
                    elapsed = self.time_arrived - self.time_call_started
                    writeLog("Arrived! (elapsed from call start: {:g} sec)".format(elapsed), self)
                    self.publishMQTT()
                elif self.state == 0:
                    elapsed = time.perf_counter() - self.time_call_started
                    writeLog("Called but did not arrived (maybe wallpad error(timeout), elapsed from call start: {:g} sec)".format(elapsed), self)
                    self.publishMQTT()
            elif self.state_prev == 1 and self.state == 0:
                writeLog("Set arrived flag", self)
                self.arrived_flag = True
        self.state_prev = self.state

        if self.arrived_flag:
            elapsed = time.perf_counter() - self.time_arrived
            if elapsed > self.time_threshold_arrived_change:
                writeLog("Clear arrived flag (elapsed from flag set: {:g} sec)".format(elapsed), self)
                self.arrived_flag = False
                self.publishMQTT()

        if not self.init:
            self.publishMQTT()
            self.init = True
    
    def getCallPacketValue(self, target: int) -> int:
        # HA 스위치는 하행 호출을 항상 6으로 보낸다 -> 옵션(elevator_packet_command_call_down_value)값으로 치환
        if target == CommandState.DOWNSIDE:
            return self.packet_command_call_down_value
        return target

    def makePacketCall(self, target: int, call_type: Union[int, None] = None) -> bytearray:
        # 상/하행 호출
        # F7 0B 01 34 02 41 10 XX 00 YY EE
        # XX: 05 = 상행, 06 = 하행 (07 = 하행 for imazu?)
        if call_type is None:
            call_type = self.packet_call_type
        packet = bytearray([0xF7, 0x0B, 0x01, 0x34])
        target_value = max(0, min(0xFF, target))
        if call_type == PacketCallType.RESPONSE:
            # 일부 환경에서는 F7 0B 01 34 04 41 10 00 XX YY EE로 호출해야 된다? (차이 파악 필요)
            # -> 복도 미니패드가 없는 세대: 월패드는 미니패드의 '호출 버튼' 응답(04)을 받아 호출하는 것으로 추정
            packet.extend([0x04, 0x41, 0x10, 0x00, target_value])
        else:
            packet.extend([0x02, 0x41, 0x10, target_value, 0x00])
        packet.append(self.calcXORChecksum(packet))
        packet.append(0xEE)
        return packet

    def makePacketCallCandidates(self, target: int) -> List[bytearray]:
        value = self.getCallPacketValue(target)
        if self.packet_call_type == PacketCallType.AUTO:
            return [self.makePacketCall(value, PacketCallType.COMMAND), self.makePacketCall(value, PacketCallType.RESPONSE)]
        return [self.makePacketCall(value)]

    """
    def makePacketCallDownside(self) -> bytearray:
        # 하행 호출
        # F7 0B 01 34 02 41 10 06 00 XX EE
        packet = bytearray([0xF7, 0x0B, 0x01, 0x34])
        if self.packet_call_type == 1:
            # 일부 환경에서는 F7 0B 01 34 04 41 10 00 06 XX EE로 호출해야 된다? (차이 파악 필요)
            packet.extend([0x04, 0x41, 0x10, 0x00, 0x06])
        else:
            packet.extend([0x02, 0x41, 0x10, 0x06, 0x00])
        packet.append(self.calcXORChecksum(packet))
        packet.append(0xEE)
        return packet

    def makePacketCallUpside(self) -> bytearray:
        # 상행 호출
        # F7 0B 01 34 02 41 10 05 00 XX EE
        packet = bytearray([0xF7, 0x0B, 0x01, 0x34])
        if self.packet_call_type == 1:
            # 일부 환경에서는 F7 0B 01 34 04 41 10 00 05 XX EE로 호출해야 된다? (차이 파악 필요)
            packet.extend([0x04, 0x41, 0x10, 0x00, 0x05])
        else:
            packet.extend([0x02, 0x41, 0x10, 0x05, 0x00])
        packet.append(self.calcXORChecksum(packet))
        packet.append(0xEE)
        return packet

    def makePacketRevokeCall(self) -> bytearray:
        packet = bytearray([0xF7, 0x0B, 0x01, 0x34])
        if self.packet_call_type == 1:
            packet.extend([0x04, 0x41, 0x10, 0x00, 0x00])
        else:
            packet.extend([0x02, 0x41, 0x10, 0x00, 0x00])
        packet.append(self.calcXORChecksum(packet))
        packet.append(0xEE)
        return packet
    """

    def check_call_command_done(self, target: int, since: float = 0.) -> bool:
        """
        호출 명령이 월패드에 반영되었는지 확인
        since: 명령 전송 시작 시각 - 이 시각 이후에 바뀐 상태만 '반영됨'으로 인정한다
               (이전 호출의 상태가 남아 있으면 새 호출을 보내지도 않고 성공 처리되던 문제 방지)
        """
        targets = {target, self.getCallPacketValue(target)}
        if self.check_command_method is CheckCommandMethod.CALL_STATE:
            if target == CommandState.IDLE:
                if self.state_call == 0:
                    writeLog("check command done: call state is idle", self)
                    return True
            elif self.state_call in targets and self.time_call_state_changed >= since:
                writeLog(f"check command done: call state is now <{self.state_call}>", self)
                return True
        elif self.check_command_method is CheckCommandMethod.DEV_CMD_STATE:
            if target == CommandState.IDLE:
                return all(info.command_state == CommandState.IDLE for info in self.dev_info_list)
            if self.time_dev_cmd_changed < since:
                return False
            for info in self.dev_info_list:
                if info.command_state in targets or info.command_state == CommandState.ACCEPTED:
                    writeLog(f"check command done: dev<{info.index}> command state is now <{info.command_state}>", self)
                    return True
        return False

    def checkStaleState(self):
        """
        상태 고착 해제 (주기적으로, 그리고 호출 명령 직전에 호출됨)
        - 미니패드 응답(04)이 끊긴 채 마지막 값이 '호출중'으로 남아 있는 경우
        - 도착/호출중 상태가 비정상적으로 오래 유지되는 경우
        """
        now = time.perf_counter()
        changed = False
        if self.state_call != 0 and now - self.time_last_response > self.call_state_stale_sec:
            writeLog(f"call state <{self.state_call}> is stale (no response packet for "
                     f"{now - self.time_last_response:.0f} sec), reset to idle", self)
            self.state_call = 0
            self.state_call_prev = 0
            self.time_call_state_changed = now
        if self.state != 0 and now - self.time_state_changed > self.state_stale_sec:
            writeLog(f"state <{self.state}> is stale ({now - self.time_state_changed:.0f} sec), reset to idle", self)
            self.state = 0
            self.state_prev = 0
            self.time_state_changed = now
            self.arrived_flag = False
            for dev in self.dev_info_list:
                dev.command_state = CommandState.IDLE
                dev.moving_state = MovingState.IDLE
                dev.floor = ''
            changed = True
        if self.arrived_flag and now - self.time_arrived > self.time_threshold_arrived_change:
            writeLog("Clear arrived flag (stale check)", self)
            self.arrived_flag = False
            changed = True
        if changed:
            self.publishMQTT()

    def startThreadStateChangeTimer(self):
        if self._thread_state_change_timer is None:
            self._thread_state_change_timer = ThreadStateChangeTimer(self, self.time_threshold_arrived_change)
            self._thread_state_change_timer.sig_action.connect(self.onThreadStateChangeTimerAction)
            self._thread_state_change_timer.sig_terminated.connect(self.onThreadStateChangeTimerTerminated)
            self._thread_state_change_timer.daemon = True
            self._thread_state_change_timer.start()
        else:
            self._thread_state_change_timer.reset()

    def stopThreadStateChangeTimer(self):
        if self._thread_state_change_timer is not None:
            self._thread_state_change_timer.stop()

    def onThreadStateChangeTimerTerminated(self):
        del self._thread_state_change_timer
        self._thread_state_change_timer = None

    def onThreadStateChangeTimerAction(self):
        writeLog("Clear arrived flag (from timer)", self)
        self.state = 0
        self.state_prev = 0
        self.time_state_changed = time.perf_counter()
        self.arrived_flag = False
        self.publishMQTT()
