# TODO: rs485 및 파서를 리스트로 객체화하게 바꾸었다
# 코드 구조를 전면적으로 개편해야 한다
from . import api
import os
import sys
import http
import datetime
from flask import render_template, jsonify, request
CURPATH = os.path.dirname(os.path.abspath(__file__))  # {$PROJECT}/web/api/
PROJPATH = os.path.dirname(os.path.dirname(CURPATH))  # {$PROJECT}/
INCPATH = os.path.join(PROJPATH, 'Include')  # {$PROJECT}/Include/
sys.path.extend([CURPATH, PROJPATH, INCPATH])
sys.path = list(set(sys.path))
del CURPATH, PROJPATH, INCPATH
from Include import get_home
from Common import DeviceType

LIGHT_LINE_DEVICES = ('light', 'doorlock', 'outlet', 'emotion light', 'dimming light')


def get_parsers(home):
    # 원작자 환경은 조명/기타/서브폰이 각각 0/1/2번 포트였지만,
    # EW11 하나만 쓰는 경우 등 parser_mapping에 맞춰 실제 파서를 찾는다
    light = home.getParser(DeviceType.LIGHT)
    various = home.getParser(DeviceType.THERMOSTAT)
    subphone = home.getParser(DeviceType.SUBPHONE)
    if subphone is not None and subphone.type_interpret.name != 'SUBPHONE':
        subphone = None
    return light, various, subphone


def filter_storage(storage, parser, other, light_side: bool):
    if parser is None:
        return []
    if other is None or parser is not other:
        return list(storage)
    # 같은 파서를 공유하는 경우 기기 종류로 나눠서 보여준다
    if light_side:
        return [x for x in storage if x.get('device') in LIGHT_LINE_DEVICES]
    return [x for x in storage if x.get('device') not in LIGHT_LINE_DEVICES]


def prettifyPacket(packet: bytearray) -> str:
    return ' '.join(['%02X' % x for x in packet])


def stringifyPacketInfo(info: dict) -> str:
    device: str = info.get('device')
    timestamp: datetime.datetime = info.get('timestamp')
    packet: bytearray = info.get('packet')

    return f" [{timestamp.strftime('%H:%M:%S.%f')[:-3]}] [{device}] {prettifyPacket(packet)}"


@api.route('/packet_logger', methods=['GET', 'POST'])
def packet_logger():
    home = get_home()
    parser_light, parser_various, parser_subphone = get_parsers(home)
    if parser_light is not None:
        enable_header_light_19 = parser_light.enable_store_packet_header_19
        enable_header_light_1E = parser_light.enable_store_packet_header_1E
        enable_header_light_1F = parser_light.enable_store_packet_header_1F
    else:
        enable_header_light_19 = False
        enable_header_light_1E = False
        enable_header_light_1F = False
    
    if parser_various is not None:
        parser = parser_various
        enable_header_various_18 = parser.enable_store_packet_header_18
        enable_header_various_1B = parser.enable_store_packet_header_1B
        enable_header_various_1C = parser.enable_store_packet_header_1C
        enable_header_various_2A = parser.enable_store_packet_header_2A
        enable_header_various_2B = parser.enable_store_packet_header_2B
        enable_header_various_34 = parser.enable_store_packet_header_34
        enable_header_various_43 = parser.enable_store_packet_header_43
        enable_header_various_44 = parser.enable_store_packet_header_44
        enable_header_various_48 = parser.enable_store_packet_header_48
    else:
        enable_header_various_18 = False
        enable_header_various_1B = False
        enable_header_various_1C = False
        enable_header_various_2A = False
        enable_header_various_2B = False
        enable_header_various_34 = False
        enable_header_various_43 = False
        enable_header_various_44 = False
        enable_header_various_48 = False
        
    if parser_subphone is not None:
        enable_subphone = parser_subphone.enable_store_packet_general
    else:
        enable_subphone = False
    
    return render_template(
        'packet_logger.html', 
        enable_header_light_19=int(enable_header_light_19),
        enable_header_light_1E=int(enable_header_light_1E),
        enable_header_light_1F=int(enable_header_light_1F),
        enable_header_various_18=int(enable_header_various_18),
        enable_header_various_1B=int(enable_header_various_1B),
        enable_header_various_1C=int(enable_header_various_1C),
        enable_header_various_2A=int(enable_header_various_2A),
        enable_header_various_2B=int(enable_header_various_2B),
        enable_header_various_34=int(enable_header_various_34),
        enable_header_various_43=int(enable_header_various_43),
        enable_header_various_44=int(enable_header_various_44),
        enable_header_various_48=int(enable_header_various_48),
        enable_subphone=int(enable_subphone)
    )


@api.route('/packet_logger/update', methods=['GET', 'POST'])
def packet_logger_update():
    home = get_home()
    parser_light, parser_various, parser_subphone = get_parsers(home)
    packets_light = filter_storage(parser_light.packet_storage, parser_light, parser_various, True) if parser_light else []
    packets_various = filter_storage(parser_various.packet_storage, parser_various, parser_light, False) if parser_various else []
    packets_subphone = list(parser_subphone.packet_storage) if parser_subphone else []
    
    str_packet_light = [stringifyPacketInfo(x) for x in packets_light[::-1]]
    str_packet_various = [stringifyPacketInfo(x) for x in packets_various[::-1]]
    str_packet_subphone = [stringifyPacketInfo(x) for x in packets_subphone[::-1]]

    return jsonify({
        'light': '<br>'.join(str_packet_light),
        'various': '<br>'.join(str_packet_various),
        'subphone': '<br>'.join(str_packet_subphone)
    })


@api.route('/packet_logger/clear/<target>', methods=['POST'])
def packet_logger_clear(target):
    home = get_home()
    parser_light, parser_various, parser_subphone = get_parsers(home)
    parser = {'light': parser_light, 'various': parser_various, 'subphone': parser_subphone}.get(target)
    if parser is not None:
        parser.clearPacketStorage()
    
    return '', http.HTTPStatus.NO_CONTENT


@api.route('/packet_logger/enable/<target>/<header>', methods=['POST'])
def packet_logger_enable_header(target, header):
    home = get_home()
    parser_light, parser_various, parser_subphone = get_parsers(home)
    req = request.get_data().decode(encoding='utf-8')
    value = int(req[6:].strip()) if 'value=' in req else 1
    if target == 'light':
        if parser_light is not None and header in ['19', '1E', '1F']:
            setattr(parser_light, f'enable_store_packet_header_{header}', bool(value))
            parser_light.clearPacketStorage()
    elif target == 'various':
        if parser_various is not None and header in ['18', '1B', '1C', '2A', '2B', '34', '43', '44', '48']:
            setattr(parser_various, f'enable_store_packet_header_{header}', bool(value))
            parser_various.clearPacketStorage()
    elif target == 'subphone':
        if parser_subphone is not None:
            parser_subphone.enable_store_packet_general = bool(value)
            parser_subphone.clearPacketStorage()
    
    return '', http.HTTPStatus.NO_CONTENT
