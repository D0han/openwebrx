import threading
import wave
from datetime import datetime, timedelta, date, timezone
import time
import sched
import subprocess
import os
from multiprocessing.connection import Pipe
from owrx.map import Map, LocatorLocation
import re
from owrx.config import PropertyManager
from owrx.bands import Bandplan

import logging
logger = logging.getLogger(__name__)


class WsjtChopper(threading.Thread):
    def __init__(self, source):
        self.source = source
        self.tmp_dir = PropertyManager.getSharedInstance()["temporary_directory"]
        (self.wavefilename, self.wavefile) = self.getWaveFile()
        self.switchingLock = threading.Lock()
        self.scheduler = sched.scheduler(time.time, time.sleep)
        self.fileQueue = []
        (self.outputReader, self.outputWriter) = Pipe()
        self.doRun = True
        super().__init__()

    def getWaveFile(self):
        filename = "{tmp_dir}/openwebrx-wsjtchopper-{id}-{timestamp}.wav".format(
            tmp_dir = self.tmp_dir,
            id = id(self),
            timestamp = datetime.utcnow().strftime(self.fileTimestampFormat)
        )
        wavefile = wave.open(filename, "wb")
        wavefile.setnchannels(1)
        wavefile.setsampwidth(2)
        wavefile.setframerate(12000)
        return (filename, wavefile)

    def getNextDecodingTime(self):
        t = datetime.now()
        zeroed = t.replace(minute=0, second=0, microsecond=0)
        delta = t - zeroed
        seconds = (int(delta.total_seconds() / self.interval) + 1) * self.interval
        t = zeroed + timedelta(seconds = seconds)
        logger.debug("scheduling: {0}".format(t))
        return t.timestamp()

    def startScheduler(self):
        self._scheduleNextSwitch()
        threading.Thread(target = self.scheduler.run).start()

    def emptyScheduler(self):
        for event in self.scheduler.queue:
            self.scheduler.cancel(event)

    def _scheduleNextSwitch(self):
        self.scheduler.enterabs(self.getNextDecodingTime(), 1, self.switchFiles)

    def switchFiles(self):
        self.switchingLock.acquire()
        file = self.wavefile
        filename = self.wavefilename
        (self.wavefilename, self.wavefile) = self.getWaveFile()
        self.switchingLock.release()

        file.close()
        self.fileQueue.append(filename)
        self._scheduleNextSwitch()

    def decoder_commandline(self, file):
        """
        must be overridden in child classes
        """
        return []

    def decode(self):
        def decode_and_unlink(file):
            decoder = subprocess.Popen(self.decoder_commandline(file), stdout=subprocess.PIPE, cwd=self.tmp_dir)
            while True:
                line = decoder.stdout.readline()
                if line is None or (isinstance(line, bytes) and len(line) == 0):
                    break
                self.outputWriter.send(line)
            rc = decoder.wait()
            logger.debug("decoder return code: %i", rc)
            os.unlink(file)

            self.decoder = decoder

        if self.fileQueue:
            file = self.fileQueue.pop()
            logger.debug("processing file {0}".format(file))
            threading.Thread(target=decode_and_unlink, args=[file]).start()

    def run(self) -> None:
        logger.debug("WSJT chopper starting up")
        self.startScheduler()
        while self.doRun:
            data = self.source.read(256)
            if data is None or (isinstance(data, bytes) and len(data) == 0):
                logger.warning("zero read on WSJT chopper")
                self.doRun = False
            else:
                self.switchingLock.acquire()
                self.wavefile.writeframes(data)
                self.switchingLock.release()

            self.decode()
        logger.debug("WSJT chopper shutting down")
        self.outputReader.close()
        self.outputWriter.close()
        self.emptyScheduler()
        try:
            os.unlink(self.wavefilename)
        except Exception:
            logger.exception("error removing undecoded file")

    def read(self):
        try:
            return self.outputReader.recv()
        except EOFError:
            return None


class Ft8Chopper(WsjtChopper):
    def __init__(self, source):
        self.interval = 15
        self.fileTimestampFormat = "%y%m%d_%H%M%S"
        super().__init__(source)

    def decoder_commandline(self, file):
        #TODO expose decoding quality parameters through config
        return ["jt9", "--ft8", "-d", "3", file]


class WsprChopper(WsjtChopper):
    def __init__(self, source):
        self.interval = 120
        self.fileTimestampFormat = "%y%m%d_%H%M"
        super().__init__(source)

    def decoder_commandline(self, file):
        #TODO expose decoding quality parameters through config
        return ["wsprd", "-d", file]


class Jt65Chopper(WsjtChopper):
    def __init__(self, source):
        self.interval = 60
        self.fileTimestampFormat = "%y%m%d_%H%M"
        super().__init__(source)

    def decoder_commandline(self, file):
        #TODO expose decoding quality parameters through config
        return ["jt9", "--jt65", "-d", "3", file]


class Jt9Chopper(WsjtChopper):
    def __init__(self, source):
        self.interval = 60
        self.fileTimestampFormat = "%y%m%d_%H%M"
        super().__init__(source)

    def decoder_commandline(self, file):
        #TODO expose decoding quality parameters through config
        return ["jt9", "--jt9", "-d", "3", file]


class Ft4Chopper(WsjtChopper):
    def __init__(self, source):
        self.interval = 7.5
        self.fileTimestampFormat = "%y%m%d_%H%M%S"
        super().__init__(source)

    def decoder_commandline(self, file):
        #TODO expose decoding quality parameters through config
        return ["jt9", "--ft4", "-d", "3", file]


class WsjtParser(object):
    locator_pattern = re.compile(".*\\s([A-Z0-9]+)\\s([A-R]{2}[0-9]{2})$")
    wspr_splitter_pattern = re.compile("([A-Z0-9]*)\\s([A-R]{2}[0-9]{2})\\s([0-9]+)")

    def __init__(self, handler):
        self.handler = handler
        self.dial_freq = None
        self.band = None

    modes = {
        "~": "FT8",
        "#": "JT65",
        "@": "JT9",
        "+": "FT4"
    }

    def parse(self, data):
        try:
            msg = data.decode().rstrip()
            # known debug messages we know to skip
            if msg.startswith("<DecodeFinished>"):
                return
            if msg.startswith(" EOF on input file"):
                return

            modes = list(WsjtParser.modes.keys())
            if msg[21] in modes or msg[19] in modes:
                out = self.parse_from_jt9(msg)
            else:
                out = self.parse_from_wsprd(msg)

            self.handler.write_wsjt_message(out)
        except ValueError:
            logger.exception("error while parsing wsjt message")

    def parse_timestamp(self, instring, dateformat):
        ts = datetime.strptime(instring, dateformat)
        return int(datetime.combine(date.today(), ts.time()).replace(tzinfo=timezone.utc).timestamp() * 1000)

    def parse_from_jt9(self, msg):
        # ft8 sample
        # '222100 -15 -0.0  508 ~  CQ EA7MJ IM66'
        # jt65 sample
        # '2352  -7  0.4 1801 #  R0WAS R2ABM KO85'
        # '0003  -4  0.4 1762 #  CQ R2ABM KO85'
        modes = list(WsjtParser.modes.keys())
        if msg[19] in modes:
            dateformat = "%H%M"
        else:
            dateformat = "%H%M%S"
        timestamp = self.parse_timestamp(msg[0:len(dateformat)], dateformat)
        msg = msg[len(dateformat) + 1:]
        modeChar = msg[14:15]
        mode = WsjtParser.modes[modeChar] if modeChar in WsjtParser.modes else "unknown"
        wsjt_msg = msg[17:53].strip()
        self.parseLocator(wsjt_msg, mode)
        return {
            "timestamp": timestamp,
            "db": float(msg[0:3]),
            "dt": float(msg[4:8]),
            "freq": int(msg[9:13]),
            "mode": mode,
            "msg": wsjt_msg
        }

    def parseLocator(self, msg, mode):
        m = WsjtParser.locator_pattern.match(msg)
        if m is None:
            return
        # this is a valid locator in theory, but it's somewhere in the arctic ocean, near the north pole, so it's very
        # likely this just means roger roger goodbye.
        if m.group(2) == "RR73":
            return
        Map.getSharedInstance().updateLocation(m.group(1), LocatorLocation(m.group(2)), mode, self.band)

    def parse_from_wsprd(self, msg):
        # wspr sample
        # '2600 -24  0.4   0.001492 -1  G8AXA JO01 33'
        # '0052 -29  2.6   0.001486  0  G02CWT IO92 23'
        wsjt_msg = msg[29:].strip()
        self.parseWsprMessage(wsjt_msg)
        return {
            "timestamp": self.parse_timestamp(msg[0:4], "%H%M"),
            "db": float(msg[5:8]),
            "dt": float(msg[9:13]),
            "freq": float(msg[14:24]),
            "drift": int(msg[25:28]),
            "mode": "WSPR",
            "msg": wsjt_msg
        }

    def parseWsprMessage(self, msg):
        m = WsjtParser.wspr_splitter_pattern.match(msg)
        if m is None:
            return
        Map.getSharedInstance().updateLocation(m.group(1), LocatorLocation(m.group(2)), "WSPR", self.band)

    def setDialFrequency(self, freq):
        self.dial_freq = freq
        self.band = Bandplan.getSharedInstance().findBand(freq)
