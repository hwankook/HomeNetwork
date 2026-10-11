#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EW11 RS-485 packet capture & diagnosis tool for Hillstate (Hyundai HT) wallpads.

- Python 3.6+ standard library only (no pip install needed)
- Connects to EW11 TCP server, frames F7 ... EE packets, verifies XOR checksum
- Prints new unique packets live, then a per-device summary and a verdict for
  thermostat (0x18) and elevator (0x34)

Usage (e.g. in Home Assistant "Terminal & SSH" add-on):
    python3 ew11_capture.py                       # 60 s capture, 192.168.0.100:8899
    python3 ew11_capture.py --seconds 180         # longer capture (operate wallpad meanwhile)
    python3 ew11_capture.py --send "F7 0B 01 34 02 41 10 06 00 ?? EE" --repeat 10
        # send a packet ('??' = auto XOR checksum) while capturing
"""
import argparse
import datetime
import os
import socket
import sys
import time
from collections import OrderedDict, defaultdict
from functools import reduce

HEADER_NAMES = {
    0x15: '감성조명',
    0x18: '난방',
    0x19: '조명',
    0x1A: '디밍조명',
    0x1B: '가스밸브',
    0x1C: '에어컨',
    0x1E: '도어락',
    0x1F: '콘센트',
    0x2A: '일괄소등',
    0x2B: '환기',
    0x34: '엘리베이터',
    0x43: 'HEMS',
    0x44: '시각',
    0x48: '??(48)',
    0x4B: '??(4B)',
}
TYPE_NAMES = {0x01: 'Q', 0x02: 'C', 0x04: 'R'}  # query / command / response
WATCH_HEADERS = (0x18, 0x34)


def hexs(data) -> str:
    return ' '.join('%02X' % b for b in data)


def xor(data) -> int:
    return reduce(lambda a, b: a ^ b, data, 0)


def parse_send_packet(text: str) -> bytearray:
    tokens = text.replace(',', ' ').split()
    out = bytearray()
    cs_pos = None
    for i, tok in enumerate(tokens):
        if tok in ('??', 'XX', 'xx', 'CS', 'cs'):
            cs_pos = i
            out.append(0)
        else:
            out.append(int(tok, 16))
    if cs_pos is not None:
        out[cs_pos] = xor(out[:cs_pos])
    return out


class Logger:
    def __init__(self, path: str):
        self.path = path
        self.fp = open(path, 'w', encoding='utf-8')

    def __call__(self, msg: str = ''):
        print(msg)
        self.fp.write(msg + '\n')
        self.fp.flush()

    def close(self):
        self.fp.close()


class Framer:
    def __init__(self):
        self.buf = bytearray()
        self.dropped = 0
        self.cs_errors = 0

    def feed(self, data: bytes):
        self.buf.extend(data)
        out = []
        while True:
            idx = self.buf.find(0xF7)
            if idx < 0:
                self.dropped += len(self.buf)
                self.buf.clear()
                break
            if idx > 0:
                self.dropped += idx
                del self.buf[:idx]
            if len(self.buf) < 2:
                break
            length = self.buf[1]
            if length < 6:
                self.dropped += 1
                del self.buf[:1]
                continue
            if len(self.buf) < length:
                if len(self.buf) > 300:
                    self.dropped += 1
                    del self.buf[:1]
                    continue
                break
            if self.buf[length - 1] != 0xEE:
                self.dropped += 1
                del self.buf[:1]
                continue
            pkt = bytes(self.buf[:length])
            del self.buf[:length]
            ok = xor(pkt[:-2]) == pkt[-2]
            if not ok:
                self.cs_errors += 1
            out.append((pkt, ok))
        return out


def decode_thermo_general(pkt: bytes, per_dev: int):
    payload = pkt[8:-2]
    if per_dev <= 0 or len(payload) % per_dev != 0:
        return None
    rooms = []
    for i in range(len(payload) // per_dev):
        chunk = payload[i * per_dev:(i + 1) * per_dev]
        rooms.append((i + 1, chunk))
    return rooms


def thermo_layout_score(pkt: bytes, per_dev: int):
    rooms = decode_thermo_general(pkt, per_dev)
    if not rooms:
        return None
    good = 0
    for _, chunk in rooms:
        state, cur, cfg = chunk[0], chunk[1], chunk[2]
        if state == 0x00:
            continue
        if state not in (0x01, 0x04, 0x07) or not (0 < cur < 60) or not (5 <= cfg <= 40):
            return None
        good += 1
    return good


def describe(pkt: bytes) -> str:
    hdr = pkt[3] if len(pkt) > 3 else -1
    typ = pkt[4] if len(pkt) > 4 else -1
    name = HEADER_NAMES.get(hdr, '미확인')
    return '[%s/%s]' % (name, TYPE_NAMES.get(typ, '%02X' % typ))


def main():
    ap = argparse.ArgumentParser(description='EW11 Hillstate RS-485 capture & diagnosis')
    ap.add_argument('--host', default='192.168.0.100')
    ap.add_argument('--port', type=int, default=8899)
    ap.add_argument('--seconds', type=int, default=60, help='capture duration (default 60)')
    ap.add_argument('--out', default=None, help='output text file')
    ap.add_argument('--send', default=None, help="hex packet to send, '??' = auto checksum")
    ap.add_argument('--repeat', type=int, default=5)
    ap.add_argument('--interval', type=float, default=0.2)
    ap.add_argument('--send-after', type=float, default=5.0, help='seconds before sending')
    ap.add_argument('--all', action='store_true', help='print every new unique packet (not only 0x18/0x34)')
    args = ap.parse_args()

    out_path = args.out or 'ew11_capture_%s.txt' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    log = Logger(out_path)
    log('# EW11 capture %s -> %s:%d, %ds' % (datetime.datetime.now().isoformat(timespec='seconds'),
                                              args.host, args.port, args.seconds))

    send_pkt = parse_send_packet(args.send) if args.send else None
    if send_pkt is not None:
        log('# send packet: %s (x%d, %.2fs interval, after %.1fs)' % (
            hexs(send_pkt), args.repeat, args.interval, args.send_after))

    try:
        sock = socket.create_connection((args.host, args.port), timeout=5)
    except OSError as e:
        log('!! EW11 접속 실패: %s' % e)
        log('   - IP/포트 확인, 또는 EW11 최대 접속 수 초과일 수 있음 → 애드온을 잠시 중지 후 재시도')
        log.close()
        sys.exit(1)
    sock.settimeout(0.5)
    log('# connected. 이제 월패드에서 난방 켜기/끄기, 온도 변경, 엘리베이터 호출을 해 보세요.')
    log('')

    framer = Framer()
    total_bytes = 0
    raw_sample = bytearray()
    counts = defaultdict(int)                 # header -> count
    counts_type = defaultdict(int)            # (header, type) -> count
    uniq = defaultdict(OrderedDict)           # header -> {packet_hex: [count, first, last]}
    t0 = time.time()
    sent = 0
    next_send = t0 + args.send_after

    try:
        while time.time() - t0 < args.seconds:
            now = time.time()
            if send_pkt is not None and sent < args.repeat and now >= next_send:
                sock.sendall(send_pkt)
                sent += 1
                next_send = now + args.interval
                log('%7.2fs  >> SEND %s' % (now - t0, hexs(send_pkt)))
            try:
                data = sock.recv(1024)
            except socket.timeout:
                continue
            if not data:
                log('!! EW11 연결 끊김')
                break
            total_bytes += len(data)
            if len(raw_sample) < 512:
                raw_sample.extend(data[:512 - len(raw_sample)])
            for pkt, ok in framer.feed(data):
                hdr = pkt[3] if len(pkt) > 3 else 0
                counts[hdr] += 1
                if len(pkt) > 4:
                    counts_type[(hdr, pkt[4])] += 1
                key = hexs(pkt)
                rec = uniq[hdr].get(key)
                t = time.time() - t0
                if rec is None:
                    uniq[hdr][key] = [1, t, t]
                    if args.all or hdr in WATCH_HEADERS or hdr not in HEADER_NAMES:
                        log('%7.2fs  NEW %-16s %s%s' % (t, describe(pkt), key, '' if ok else '  (checksum ERR)'))
                else:
                    rec[0] += 1
                    rec[2] = t
    except KeyboardInterrupt:
        log('# interrupted')
    finally:
        sock.close()

    elapsed = time.time() - t0
    log('')
    log('=' * 72)
    log('# 요약: %.0fs, %d bytes, 패킷 %d개, 버린 바이트 %d, 체크섬 오류 %d' % (
        elapsed, total_bytes, sum(counts.values()), framer.dropped, framer.cs_errors))
    log('=' * 72)
    for hdr in sorted(counts):
        types = ', '.join('%s=%d' % (TYPE_NAMES.get(t, '%02X' % t), c)
                          for (h, t), c in sorted(counts_type.items()) if h == hdr)
        log('  0x%02X %-8s %6d개  고유 %3d  (%s)' % (hdr, HEADER_NAMES.get(hdr, '미확인'), counts[hdr],
                                                 len(uniq[hdr]), types))

    if total_bytes > 0 and sum(counts.values()) == 0:
        log('')
        log('!! F7...EE 형식 패킷이 하나도 없음 → 프로토콜/통신속도(9600 8N1) 불일치 가능. raw 샘플:')
        log('   ' + hexs(raw_sample[:128]))

    # ---- full unique packet dump for watched headers ----
    for hdr in WATCH_HEADERS:
        log('')
        log('-' * 72)
        log('# 0x%02X %s 고유 패킷 (횟수, 처음~마지막 s)' % (hdr, HEADER_NAMES[hdr]))
        if not uniq[hdr]:
            log('  (없음)')
        for key, (c, first, last) in uniq[hdr].items():
            log('  %5d  %6.1f~%6.1f  %s' % (c, first, last, key))

    # ---- verdict ----
    log('')
    log('=' * 72)
    log('# 판정')
    log('=' * 72)
    light_ok = counts.get(0x19, 0) > 0
    if light_ok and counts.get(0x18, 0) == 0 and counts.get(0x34, 0) == 0:
        log('* 조명(0x19)은 보이는데 난방(0x18)·엘리베이터(0x34)가 0개입니다.')
        log('  → 이 EW11은 "조명/콘센트" RS-485 라인에만 연결된 것으로 보입니다.')
        log('    월패드의 다른 RS-485 라인(난방·가스·환기·엘리베이터)에 EW11을 하나 더 연결하고')
        log('    애드온 rs485에 port2(index 1)를 추가, parser_mapping의 thermostat/elevator 등을 1로 설정하세요.')
        log('    (코드 수정만으로는 해결 불가)')

    # thermostat
    t_pk = [bytes.fromhex(k.replace(' ', '')) for k in uniq[0x18]]
    if t_pk:
        general = [p for p in t_pk if len(p) > 10 and p[4] == 0x04 and (p[6] & 0x0F) == 0]
        if general:
            p = max(general, key=len)
            best = []
            for per_dev in (3, 8, 4, 5, 6, 7):
                score = thermo_layout_score(p, per_dev)
                if score:
                    best.append((per_dev, score))
            log('* 난방 상태 패킷 예: %s' % hexs(p))
            if best:
                per_dev = best[0][0]
                log('  → 기기당 %d바이트 배열이 맞음 (애드온 etc.thermo_len_per_dev = %d)' % (per_dev, per_dev))
                for room, chunk in decode_thermo_general(p, per_dev):
                    if chunk[0] == 0:
                        continue
                    log('    방 %d: %s, 현재 %d℃, 설정 %d℃  [%s]' % (
                        room, 'OFF' if chunk[0] == 0x04 else 'ON(%02X)' % chunk[0], chunk[1], chunk[2], hexs(chunk)))
            else:
                log('  → 3/8바이트 어느 배열로도 해석 안 됨: 이 패킷을 그대로 보내주세요.')
        else:
            log('* 난방 패킷은 있으나 전체 상태응답(04, 방 0) 형식이 아님 → 위 고유 패킷 목록을 보내주세요.')
        cmds = [p for p in t_pk if len(p) > 6 and p[4] == 0x02]
        for p in cmds:
            log('  월패드가 보낸 난방 명령: %s' % hexs(p))

    # elevator
    e_pk = [bytes.fromhex(k.replace(' ', '')) for k in uniq[0x34]]
    if e_pk:
        q = [p for p in e_pk if p[4] == 0x01]
        r = [p for p in e_pk if p[4] == 0x04]
        c = [p for p in e_pk if p[4] == 0x02]
        log('* 엘리베이터: Q(월패드→미니패드) %d종, R(미니패드→월패드) %d종, C(명령) %d종' % (len(q), len(r), len(c)))
        if r:
            log('  → 미니패드 응답(04)이 버스에 있음: elevator_packet_call_type = 0 부터 시도')
        else:
            log('  → 미니패드 응답(04)이 없음: elevator_packet_call_type = 1 (미니패드 흉내) 부터 시도')
        for p in c:
            log('  관측된 호출 명령: %s' % hexs(p))
        lens = sorted(set(len(p) for p in e_pk))
        if any(l != 0x0B and l != 0x0D for l in lens):
            log('  !! 예상과 다른 길이의 엘리베이터 패킷 존재 (%s) → 이 파일을 보내주세요.' % lens)

    unknown = [h for h in counts if h not in HEADER_NAMES]
    if unknown:
        log('* 코드가 모르는 헤더: %s → 이 기기들은 현재 무시되고 있음' % ', '.join('0x%02X' % h for h in unknown))

    log('')
    log('# 결과 파일: %s' % os.path.abspath(out_path))
    log.close()


if __name__ == '__main__':
    main()
